"""ImmutableBudgetStateMachine — closed taxonomy, caps, monotonic-tightening.

Slice Aegis-1 regression spine, claim #2 (the budget half): per-route +
session + hourly caps enforce as strictest-wins, and tighten() rejects
loosening attempts.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.aegis.budget_state_machine import (
    BudgetCaps,
    BudgetVerdict,
    HOURLY_BURN_WINDOW_S,
    ImmutableBudgetStateMachine,
    KNOWN_ROUTES,
    MonotonicTighteningViolationError,
    RejectReason,
)


# ---------------------------------------------------------------------------
# Closed-taxonomy pins
# ---------------------------------------------------------------------------


def test_reject_reason_closed_taxonomy_exact_membership():
    """AST-style pin: the 6 values match §43.6.1 spec, bytes-identical."""
    expected = {
        "emission_cap_exceeded",
        "fanout_cap_exceeded",
        "cost_ceiling_exceeded",
        "causal_depth_exceeded",
        "lineage_forgery",
        "budget_authority_unavailable",
    }
    actual = {r.value for r in RejectReason}
    assert actual == expected, (
        f"RejectReason taxonomy drifted. extra={actual - expected}, "
        f"missing={expected - actual}"
    )


def test_known_routes_exact_membership():
    expected = ("IMMEDIATE", "STANDARD", "COMPLEX", "BACKGROUND", "SPECULATIVE")
    assert KNOWN_ROUTES == expected


def test_budget_verdict_dict_roundtrip_admitted():
    v = BudgetVerdict(
        admitted=True, reason=None,
        debit_usd=0.05,
        remaining_session_usd=0.95,
        remaining_hourly_usd=0.45,
        remaining_route_usd=0.25,
    )
    d = v.to_dict()
    recovered = BudgetVerdict.from_dict(d)
    assert recovered == v


def test_budget_verdict_dict_roundtrip_denied():
    v = BudgetVerdict(
        admitted=False,
        reason=RejectReason.COST_CEILING_EXCEEDED,
        debit_usd=0.0,
        remaining_session_usd=0.0,
        remaining_hourly_usd=0.0,
        remaining_route_usd=0.0,
        detail="session cap exceeded",
    )
    d = v.to_dict()
    recovered = BudgetVerdict.from_dict(d)
    assert recovered == v


# ---------------------------------------------------------------------------
# Admit + caps
# ---------------------------------------------------------------------------


def _machine(
    tmp_path: Path,
    *,
    session_cap: float = 1.00,
    hourly_cap: float = 0.50,
    route_caps: dict | None = None,
    overrun: float = 1.0,
) -> ImmutableBudgetStateMachine:
    caps = BudgetCaps(
        session_cap_usd=session_cap,
        hourly_burn_cap_usd=hourly_cap,
        route_caps_usd=route_caps if route_caps is not None else {
            "STANDARD": 0.25, "IMMEDIATE": 0.50,
        },
        overrun_multiplier=overrun,
    )
    return ImmutableBudgetStateMachine(
        caps=caps, wal_path=tmp_path / "spend.jsonl",
    )


@pytest.mark.asyncio
async def test_admit_succeeds_within_caps(tmp_path):
    m = _machine(tmp_path)
    v = await m.admit(
        route="STANDARD", estimated_cost_usd=0.10,
        lease_nonce="n1", op_id="op-1",
    )
    assert v.admitted is True
    assert v.reason is None
    assert v.debit_usd == pytest.approx(0.10)


@pytest.mark.asyncio
async def test_admit_rejected_when_route_cap_exceeded(tmp_path):
    m = _machine(tmp_path, route_caps={"STANDARD": 0.05})
    v = await m.admit(
        route="STANDARD", estimated_cost_usd=0.10,
        lease_nonce="n1", op_id="op-1",
    )
    assert v.admitted is False
    assert v.reason is RejectReason.COST_CEILING_EXCEEDED
    assert "route" in (v.detail or "").lower()


@pytest.mark.asyncio
async def test_admit_rejected_when_session_cap_exceeded(tmp_path):
    m = _machine(tmp_path, session_cap=0.05, hourly_cap=1.00,
                 route_caps={"STANDARD": 1.00})
    v = await m.admit(
        route="STANDARD", estimated_cost_usd=0.10,
        lease_nonce="n1", op_id="op-1",
    )
    assert v.admitted is False
    assert v.reason is RejectReason.COST_CEILING_EXCEEDED
    assert "session" in (v.detail or "").lower()


@pytest.mark.asyncio
async def test_admit_rejected_when_hourly_cap_exceeded(tmp_path):
    m = _machine(tmp_path, session_cap=10.0, hourly_cap=0.05,
                 route_caps={"STANDARD": 10.0})
    v = await m.admit(
        route="STANDARD", estimated_cost_usd=0.10,
        lease_nonce="n1", op_id="op-1",
    )
    assert v.admitted is False
    assert v.reason is RejectReason.COST_CEILING_EXCEEDED
    assert "hourly" in (v.detail or "").lower()


@pytest.mark.asyncio
async def test_strictest_wins_route_blocks_when_others_open(tmp_path):
    """Route cap is the tightest — that's the failure reason cited."""
    m = _machine(
        tmp_path, session_cap=10.0, hourly_cap=10.0,
        route_caps={"STANDARD": 0.05},
    )
    v = await m.admit(
        route="STANDARD", estimated_cost_usd=0.10,
        lease_nonce="n1", op_id="op-1",
    )
    assert v.admitted is False
    assert "route" in (v.detail or "").lower()


@pytest.mark.asyncio
async def test_admit_negative_estimate_rejected(tmp_path):
    m = _machine(tmp_path)
    v = await m.admit(
        route="STANDARD", estimated_cost_usd=-0.01,
        lease_nonce="n1", op_id="op-1",
    )
    assert v.admitted is False
    assert v.reason is RejectReason.COST_CEILING_EXCEEDED


@pytest.mark.asyncio
async def test_admit_route_without_configured_cap_unbounded(tmp_path):
    """A route absent from route_caps is treated as unbounded; only
    session + hourly apply."""
    m = _machine(tmp_path, route_caps={"STANDARD": 0.05})
    # COMPLEX has no cap in the map → only session + hourly enforce.
    v = await m.admit(
        route="COMPLEX", estimated_cost_usd=0.30,
        lease_nonce="n1", op_id="op-1",
    )
    assert v.admitted is True


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_refunds_when_actual_below_reserve(tmp_path):
    m = _machine(tmp_path, session_cap=1.0, hourly_cap=1.0,
                 route_caps={"STANDARD": 1.0}, overrun=2.0)
    v_admit = await m.admit(
        route="STANDARD", estimated_cost_usd=0.10,
        lease_nonce="n1", op_id="op-1",
    )
    assert v_admit.admitted is True
    assert v_admit.debit_usd == pytest.approx(0.20)  # 0.10 × 2.0

    v_reconcile = await m.reconcile(
        lease_nonce="n1", op_id="op-1", route="STANDARD",
        actual_cost_usd=0.08,
    )
    assert v_reconcile.admitted is True
    snap = m.snapshot()
    # Reserve 0.20 - 0.12 refund == 0.08 actual on session
    assert snap["session_debit_usd"] == pytest.approx(0.08)


@pytest.mark.asyncio
async def test_reconcile_unknown_lease_returns_budget_authority_unavailable(tmp_path):
    m = _machine(tmp_path)
    v = await m.reconcile(
        lease_nonce="never-admitted", op_id="op-x", route="STANDARD",
        actual_cost_usd=0.05,
    )
    assert v.admitted is False
    assert v.reason is RejectReason.BUDGET_AUTHORITY_UNAVAILABLE


# ---------------------------------------------------------------------------
# Monotonic-tightening
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tighten_session_cap_accepted_when_lower(tmp_path):
    m = _machine(tmp_path, session_cap=1.00)
    new_caps = await m.tighten(session_cap_usd=0.50)
    assert new_caps.session_cap_usd == 0.50


@pytest.mark.asyncio
async def test_tighten_session_cap_rejected_when_equal(tmp_path):
    m = _machine(tmp_path, session_cap=1.00)
    with pytest.raises(MonotonicTighteningViolationError):
        await m.tighten(session_cap_usd=1.00)


@pytest.mark.asyncio
async def test_tighten_session_cap_rejected_when_higher(tmp_path):
    m = _machine(tmp_path, session_cap=1.00)
    with pytest.raises(MonotonicTighteningViolationError):
        await m.tighten(session_cap_usd=2.00)


@pytest.mark.asyncio
async def test_tighten_hourly_cap_rejected_when_higher(tmp_path):
    m = _machine(tmp_path, hourly_cap=0.50)
    with pytest.raises(MonotonicTighteningViolationError):
        await m.tighten(hourly_burn_cap_usd=0.75)


@pytest.mark.asyncio
async def test_tighten_route_cap_rejected_when_higher(tmp_path):
    m = _machine(tmp_path, route_caps={"STANDARD": 0.10})
    with pytest.raises(MonotonicTighteningViolationError):
        await m.tighten(route_caps_usd={"STANDARD": 0.20})


@pytest.mark.asyncio
async def test_tighten_overrun_multiplier_rejected_when_higher(tmp_path):
    m = _machine(tmp_path, overrun=1.5)
    with pytest.raises(MonotonicTighteningViolationError):
        await m.tighten(overrun_multiplier=2.0)


@pytest.mark.asyncio
async def test_tighten_adding_new_route_cap_is_tightening(tmp_path):
    """Setting a cap on a previously-unbounded route is tightening
    (unset = unlimited, set = bounded), so it's accepted."""
    m = _machine(tmp_path, route_caps={"STANDARD": 0.10})
    new_caps = await m.tighten(route_caps_usd={"BACKGROUND": 0.05})
    assert new_caps.route_caps_usd["BACKGROUND"] == 0.05
    assert new_caps.route_caps_usd["STANDARD"] == 0.10  # untouched


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_includes_all_caps(tmp_path):
    m = _machine(tmp_path)
    snap = m.snapshot()
    for key in (
        "session_cap_usd", "session_debit_usd",
        "hourly_burn_cap_usd", "hourly_burn_used_usd",
        "route_caps_usd", "route_debit_usd",
        "open_reserve_count", "overrun_multiplier",
        "schema_version",
    ):
        assert key in snap


def test_hourly_window_constant_matches_spec():
    """HOURLY_BURN_WINDOW_S must be 3600 (1 hour) per §43.6.1."""
    assert HOURLY_BURN_WINDOW_S == 3600
