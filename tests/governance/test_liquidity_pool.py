"""Dynamic Liquidity Pool — keep background autonomy alive during a DW-RT outage.

When DW realtime is ENTIRELY entitlement-denied (DIRECT_STREAMING → AUTH_FAILED),
the DW-only SPECULATIVE/BACKGROUND lanes starve. The pool issues a bounded,
budget-gated, instantly-revocable lease elevating such an op to STANDARD (Claude-
capable). These tests pin the 3-way truth table, the micro-ceiling, per-op dedup,
instant revoke, the reused real signals, and the classify() integration.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance import liquidity_pool as lp
from backend.core.ouroboros.governance.urgency_router import (
    ProviderRoute,
    UrgencyRouter,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in (
        "JARVIS_LIQUIDITY_POOL_ENABLED", "JARVIS_LIQUIDITY_MIN_BUDGET_RATIO",
        "JARVIS_LIQUIDITY_MICRO_CEILING_USD", "JARVIS_LIQUIDITY_PER_OP_ESTIMATE_USD",
    ):
        monkeypatch.delenv(var, raising=False)
    lp._reset_for_tests()
    yield
    lp._reset_for_tests()


def _dw(monkeypatch, *, denied, ratio):
    """Force the two reused signals to the requested state."""
    monkeypatch.setattr(lp, "_dw_rt_denied", lambda: denied)
    monkeypatch.setattr(lp, "_unspent_budget_ratio", lambda: ratio)


# ===========================================================================
# A. The 3-way truth table (the mandate's core assertion)
# ===========================================================================


def test_deny_when_dw_healthy(monkeypatch):
    _dw(monkeypatch, denied=False, ratio=0.99)          # DW healthy, budget full
    assert lp.should_elevate(route_value="speculative", op_id="a") is None


def test_deny_when_dw_down_but_budget_tight(monkeypatch):
    _dw(monkeypatch, denied=True, ratio=0.2)            # DW down, budget < 0.5
    assert lp.should_elevate(route_value="speculative", op_id="a") is None


def test_grant_when_dw_down_and_budget_abundant(monkeypatch):
    _dw(monkeypatch, denied=True, ratio=0.8)            # DW down, budget > 0.5
    assert lp.should_elevate(route_value="speculative", op_id="a") == "dw_rt_denied"


def test_background_route_also_eligible(monkeypatch):
    _dw(monkeypatch, denied=True, ratio=0.8)
    assert lp.should_elevate(route_value="background", op_id="a") == "dw_rt_denied"


def test_exact_boundary_ratio_grants(monkeypatch):
    # ratio == min (0.5): the gate is `< min` → deny, so exactly-at-threshold GRANTS.
    _dw(monkeypatch, denied=True, ratio=0.5)
    assert lp.should_elevate(route_value="speculative", op_id="a") == "dw_rt_denied"


# ===========================================================================
# B. Eligibility + master switch
# ===========================================================================


@pytest.mark.parametrize("route", ["standard", "immediate", "complex", "informational", ""])
def test_non_eligible_routes_never_elevate(monkeypatch, route):
    _dw(monkeypatch, denied=True, ratio=0.9)            # even under grant conditions
    assert lp.should_elevate(route_value=route, op_id="a") is None


def test_disabled_pool_never_elevates(monkeypatch):
    monkeypatch.setenv("JARVIS_LIQUIDITY_POOL_ENABLED", "false")
    _dw(monkeypatch, denied=True, ratio=0.9)
    assert lp.should_elevate(route_value="speculative", op_id="a") is None


# ===========================================================================
# C. Micro-ceiling + per-op dedup + instant revoke
# ===========================================================================


def test_micro_ceiling_stops_further_leases(monkeypatch):
    monkeypatch.setenv("JARVIS_LIQUIDITY_MICRO_CEILING_USD", "0.10")
    monkeypatch.setenv("JARVIS_LIQUIDITY_PER_OP_ESTIMATE_USD", "0.05")
    _dw(monkeypatch, denied=True, ratio=0.9)
    assert lp.should_elevate(route_value="speculative", op_id="o1") == "dw_rt_denied"  # $0.05
    assert lp.should_elevate(route_value="speculative", op_id="o2") == "dw_rt_denied"  # $0.10
    # third would reserve $0.15 > $0.10 ceiling → denied
    assert lp.should_elevate(route_value="speculative", op_id="o3") is None


def test_per_op_dedup_does_not_double_charge(monkeypatch):
    monkeypatch.setenv("JARVIS_LIQUIDITY_MICRO_CEILING_USD", "0.06")
    monkeypatch.setenv("JARVIS_LIQUIDITY_PER_OP_ESTIMATE_USD", "0.05")
    _dw(monkeypatch, denied=True, ratio=0.9)
    # Same op re-classified 3× → charged ONCE; a NEW op still fits within ceiling.
    assert lp.should_elevate(route_value="speculative", op_id="o1") == "dw_rt_denied"
    assert lp.should_elevate(route_value="speculative", op_id="o1") == "dw_rt_denied"
    assert lp.should_elevate(route_value="speculative", op_id="o1") == "dw_rt_denied"
    assert lp.stats()["reserved_usd"] == pytest.approx(0.05)
    assert lp.stats()["leased_ops"] == 1


def test_instant_revoke_on_dw_recovery(monkeypatch):
    _dw(monkeypatch, denied=True, ratio=0.9)
    assert lp.should_elevate(route_value="speculative", op_id="o1") == "dw_rt_denied"
    # DW recovers → the very next evaluation declines (no new lease).
    monkeypatch.setattr(lp, "_dw_rt_denied", lambda: False)
    assert lp.should_elevate(route_value="speculative", op_id="o2") is None


def test_never_raises_on_garbage(monkeypatch):
    _dw(monkeypatch, denied=True, ratio=0.9)
    assert lp.should_elevate(route_value=None, op_id=None) is None  # type: ignore[arg-type]


# ===========================================================================
# D. The reused REAL signals (not mocked) — prove the DRY wiring reads truth
# ===========================================================================


def test_real_dw_rt_denied_reads_direct_streaming_auth_failed(monkeypatch):
    """_dw_rt_denied must read provider_availability's dw_healthy/dw_reason."""
    from backend.core.ouroboros.governance import liquidity_pool as L
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.provider_availability.collect_provider_availability",
        lambda *a, **k: SimpleNamespace(dw_healthy=False, dw_reason="auth_failed"),
    )
    assert L._dw_rt_denied() is True
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.provider_availability.collect_provider_availability",
        lambda *a, **k: SimpleNamespace(dw_healthy=False, dw_reason="transport_degraded"),
    )
    assert L._dw_rt_denied() is False       # unhealthy but NOT an entitlement 403
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.provider_availability.collect_provider_availability",
        lambda *a, **k: SimpleNamespace(dw_healthy=True, dw_reason="healthy"),
    )
    assert L._dw_rt_denied() is False


def test_real_budget_ratio_reads_session_authority(monkeypatch):
    """_unspent_budget_ratio must compute remaining/total from the SBA."""
    from backend.core.ouroboros.governance import session_budget_authority as sba
    prov = SimpleNamespace(remaining=8.0, total_spent=2.0)   # 8 unspent of 10 → 0.8
    sba.set_session_budget_provider(prov)
    try:
        assert lp._unspent_budget_ratio() == pytest.approx(0.8, abs=0.01)
    finally:
        sba.set_session_budget_provider(None)
    # No authority registered → fail-soft to 1.0 (full budget).
    assert lp._unspent_budget_ratio() == pytest.approx(1.0)


# ===========================================================================
# E. Integration — UrgencyRouter.classify elevates SPECULATIVE → STANDARD
# ===========================================================================


def test_classify_elevates_speculative_to_standard(monkeypatch):
    # Isolate the matrix (skip the value-band layer) so we get a clean SPECULATIVE.
    monkeypatch.setenv("JARVIS_SIGNAL_VALUE_ROUTING_ENABLED", "false")
    _dw(monkeypatch, denied=True, ratio=0.9)
    ctx = SimpleNamespace(
        signal_urgency="low", signal_source="intent_discovery",
        task_complexity="simple", target_files=["a.py"], cross_repo=False,
        provider_route="", provider_route_reason="", op_id="op-e2e",
    )
    route, reason = UrgencyRouter().classify(ctx)
    assert route is ProviderRoute.STANDARD
    assert reason.startswith("liquidity_lease:speculative_to_standard:dw_rt_denied:")
    # ...and the original speculative rationale is preserved as the suffix.
    assert "speculative_source:intent_discovery" in reason


def test_classify_no_elevation_when_dw_healthy(monkeypatch):
    monkeypatch.setenv("JARVIS_SIGNAL_VALUE_ROUTING_ENABLED", "false")
    _dw(monkeypatch, denied=False, ratio=0.9)           # DW healthy → no lease
    ctx = SimpleNamespace(
        signal_urgency="low", signal_source="intent_discovery",
        task_complexity="simple", target_files=["a.py"], cross_repo=False,
        provider_route="", provider_route_reason="", op_id="op-x",
    )
    route, _ = UrgencyRouter().classify(ctx)
    assert route is ProviderRoute.SPECULATIVE     # unchanged — legacy DW-only
