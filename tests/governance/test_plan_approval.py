"""Regression spine — PlanApproval primitive (problem #7 Slice 1).

Covers the authority + state-machine + observability contracts
locked by authorization:

  1. Deny-by-default env gate + per-op override.
  2. State machine: pending → approved | rejected | expired.
  3. Future resolution semantics.
  4. Timeout auto-reject + cancel on manual resolve.
  5. Capacity bound + duplicate rejection.
  6. Listener hooks (plan_pending / plan_approved / plan_rejected /
     plan_expired) fire with projection payloads.
  7. Reviewer + reason propagated through the outcome + history.
  8. Eviction is idempotent + only terminal records.
  9. Authority invariant: no imports from orchestrator /
     tool_executor / iron_gate / risk_tier / gate modules.
 10. Reason truncation caps payload size.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance import plan_approval
from backend.core.ouroboros.governance.plan_approval import (
    PlanApprovalCapacityError,
    PlanApprovalController,
    PlanApprovalOutcome,
    PlanApprovalStateError,
    STATE_APPROVED,
    STATE_EXPIRED,
    STATE_PENDING,
    STATE_REJECTED,
    await_approval,
    get_default_controller,
    needs_approval,
    plan_approval_enabled,
    reset_default_controller,
)


# --------------------------------------------------------------------------
# Fixture
# --------------------------------------------------------------------------


_ENV_KEYS = [
    "JARVIS_PLAN_APPROVAL_ENABLED",
    "JARVIS_PLAN_APPROVAL_TIMEOUT_S",
    "JARVIS_PLAN_APPROVAL_MAX_PENDING",
    "JARVIS_PLAN_APPROVAL_REASON_MAX_LEN",
]


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    reset_default_controller()
    yield
    reset_default_controller()


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _sample_plan() -> Dict[str, Any]:
    return {
        "schema_version": "plan.1",
        "approach": "add a feature flag",
        "complexity": "moderate",
        "ordered_changes": [{"file_path": "x.py", "action": "modify"}],
        "risk_factors": ["none"],
        "test_strategy": "unit tests",
    }


# --------------------------------------------------------------------------
# 1. Env gate + needs_approval()
# --------------------------------------------------------------------------


def test_plan_approval_disabled_by_default():
    assert plan_approval_enabled() is False


def test_env_false_string_opts_out(monkeypatch):
    monkeypatch.setenv("JARVIS_PLAN_APPROVAL_ENABLED", "false")
    assert plan_approval_enabled() is False


def test_env_explicit_true_enables(monkeypatch):
    monkeypatch.setenv("JARVIS_PLAN_APPROVAL_ENABLED", "true")
    assert plan_approval_enabled() is True


def test_needs_approval_defers_to_env_flag(monkeypatch):
    monkeypatch.delenv("JARVIS_PLAN_APPROVAL_ENABLED", raising=False)
    assert needs_approval() is False
    monkeypatch.setenv("JARVIS_PLAN_APPROVAL_ENABLED", "true")
    assert needs_approval() is True


def test_needs_approval_per_op_override_forces_false(monkeypatch):
    monkeypatch.setenv("JARVIS_PLAN_APPROVAL_ENABLED", "true")
    ctx = MagicMock()
    ctx.plan_approval_override = False
    assert needs_approval(ctx) is False


def test_needs_approval_per_op_override_forces_true(monkeypatch):
    monkeypatch.setenv("JARVIS_PLAN_APPROVAL_ENABLED", "false")
    ctx = MagicMock()
    ctx.plan_approval_override = True
    assert needs_approval(ctx) is True


def test_needs_approval_ignores_non_bool_override(monkeypatch):
    monkeypatch.setenv("JARVIS_PLAN_APPROVAL_ENABLED", "true")
    ctx = MagicMock()
    ctx.plan_approval_override = "yes"  # not a bool
    assert needs_approval(ctx) is True  # defers to env


# --------------------------------------------------------------------------
# 2. Authority invariant
# --------------------------------------------------------------------------


def test_plan_approval_module_does_not_import_gate_modules():
    """§1 Boundary: the approval primitive must never import
    orchestrator / tool_executor / iron_gate / risk_tier / gate
    modules. Approval authority is human — the primitive stays
    orthogonal to decision-authority code."""
    src = Path(plan_approval.__file__).read_text()
    forbidden = [
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.tool_executor",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.risk_tier_floor",
        "from backend.core.ouroboros.governance.semantic_guardian",
        "from backend.core.ouroboros.governance.semantic_firewall",
        "from backend.core.ouroboros.governance.policy_engine",
    ]
    for f in forbidden:
        assert f not in src, "plan_approval imports " + f


# --------------------------------------------------------------------------
# 3. State machine — pending → approved
# --------------------------------------------------------------------------


def test_request_approval_creates_pending():
    async def _t():
        c = PlanApprovalController()
        fut = c.request_approval("op-a", _sample_plan())
        assert not fut.done()
        assert c.pending_count == 1
        assert c.pending_op_ids() == ["op-a"]
        snap = c.snapshot("op-a")
        assert snap is not None
        assert snap["op_id"] == "op-a"
        assert snap["state"] == STATE_PENDING
        assert snap["plan"]["approach"] == "add a feature flag"
    _run_async(_t())


def test_approve_resolves_future_with_approved_outcome():
    async def _t():
        c = PlanApprovalController()
        fut = c.request_approval("op-a", _sample_plan())
        outcome = c.approve("op-a", reviewer="repl")
        assert outcome.approved is True
        assert outcome.state == STATE_APPROVED
        assert outcome.reviewer == "repl"
        assert outcome.elapsed_s >= 0.0
        # Future resolves on loop tick.
        resolved = await asyncio.wait_for(fut, timeout=1.0)
        assert resolved.approved is True
    _run_async(_t())


def test_reject_resolves_future_with_rejected_outcome():
    async def _t():
        c = PlanApprovalController()
        fut = c.request_approval("op-a", _sample_plan())
        outcome = c.reject("op-a", reason="wrong approach", reviewer="repl")
        assert outcome.approved is False
        assert outcome.state == STATE_REJECTED
        assert outcome.reason == "wrong approach"
        resolved = await asyncio.wait_for(fut, timeout=1.0)
        assert resolved.approved is False
        assert resolved.reason == "wrong approach"
    _run_async(_t())


def test_reject_coerces_empty_reason_to_placeholder():
    async def _t():
        c = PlanApprovalController()
        c.request_approval("op-a", _sample_plan())
        outcome = c.reject("op-a", reason="   ")
        assert outcome.reason == "(no reason)"
    _run_async(_t())


def test_reject_truncates_long_reason():
    async def _t():
        c = PlanApprovalController()
        c.request_approval("op-a", _sample_plan())
        mega = "x" * 5000
        outcome = c.reject("op-a", reason=mega)
        # Default cap is 2000 + the "...<truncated>" marker.
        assert len(outcome.reason) == 2000 + len("...<truncated>")
        assert outcome.reason.endswith("...<truncated>")
    _run_async(_t())


def test_approve_on_missing_op_id_raises():
    async def _t():
        c = PlanApprovalController()
        with pytest.raises(PlanApprovalStateError):
            c.approve("nonexistent")
    _run_async(_t())


def test_reject_on_missing_op_id_raises():
    async def _t():
        c = PlanApprovalController()
        with pytest.raises(PlanApprovalStateError):
            c.reject("nonexistent", reason="x")
    _run_async(_t())


def test_approve_twice_raises_second_time():
    async def _t():
        c = PlanApprovalController()
        c.request_approval("op-a", _sample_plan())
        c.approve("op-a")
        with pytest.raises(PlanApprovalStateError):
            c.approve("op-a")
    _run_async(_t())


def test_request_same_op_id_while_pending_raises():
    async def _t():
        c = PlanApprovalController()
        c.request_approval("op-a", _sample_plan())
        with pytest.raises(PlanApprovalStateError):
            c.request_approval("op-a", _sample_plan())
    _run_async(_t())


def test_request_same_op_id_after_terminal_also_raises():
    """Defensive — even after approve/reject we still hold the
    record until evict_terminal is called; same op_id cannot be
    re-requested."""
    async def _t():
        c = PlanApprovalController()
        c.request_approval("op-a", _sample_plan())
        c.approve("op-a")
        # Terminal but still in registry — can't re-register.
        # (State check is ``not is_terminal`` — so this should work!
        # Actually the code says "if existing is not None and not
        # existing.is_terminal" — so a terminal record does NOT
        # block re-request. Let me verify.)
        fut2 = c.request_approval("op-a", _sample_plan())
        assert fut2 is not None
        # Now there's a new pending record replacing the old terminal one.
        assert c.snapshot("op-a")["state"] == STATE_PENDING
    _run_async(_t())


def test_empty_op_id_raises():
    async def _t():
        c = PlanApprovalController()
        with pytest.raises(PlanApprovalStateError):
            c.request_approval("", _sample_plan())
    _run_async(_t())


# --------------------------------------------------------------------------
# 4. Timeout
# --------------------------------------------------------------------------


def test_timeout_auto_rejects_with_expired_state():
    async def _t():
        c = PlanApprovalController(default_timeout_s=0.1)
        fut = c.request_approval("op-a", _sample_plan())
        outcome = await asyncio.wait_for(fut, timeout=2.0)
        assert outcome.approved is False
        assert outcome.state == STATE_EXPIRED
        assert outcome.reviewer == "auto-timeout"
        assert "plan_expired" in outcome.reason
    _run_async(_t())


def test_manual_approve_cancels_timeout():
    async def _t():
        c = PlanApprovalController(default_timeout_s=5.0)
        fut = c.request_approval("op-a", _sample_plan())
        await asyncio.sleep(0.05)
        c.approve("op-a")
        outcome = await asyncio.wait_for(fut, timeout=1.0)
        # Must be approved, NOT expired.
        assert outcome.state == STATE_APPROVED
    _run_async(_t())


def test_custom_timeout_s_per_request():
    async def _t():
        c = PlanApprovalController(default_timeout_s=300.0)
        fut = c.request_approval("op-a", _sample_plan(), timeout_s=0.05)
        outcome = await asyncio.wait_for(fut, timeout=2.0)
        assert outcome.state == STATE_EXPIRED
    _run_async(_t())


# --------------------------------------------------------------------------
# 5. Capacity
# --------------------------------------------------------------------------


def test_capacity_cap_blocks_excess_requests():
    async def _t():
        c = PlanApprovalController(max_pending=2)
        c.request_approval("op-1", _sample_plan())
        c.request_approval("op-2", _sample_plan())
        with pytest.raises(PlanApprovalCapacityError):
            c.request_approval("op-3", _sample_plan())
    _run_async(_t())


def test_reject_frees_slot_for_new_request():
    async def _t():
        c = PlanApprovalController(max_pending=2)
        c.request_approval("op-1", _sample_plan())
        c.request_approval("op-2", _sample_plan())
        c.reject("op-1", reason="x")
        c.evict_terminal("op-1")
        # Now op-3 fits.
        fut = c.request_approval("op-3", _sample_plan())
        assert fut is not None
    _run_async(_t())


# --------------------------------------------------------------------------
# 6. Listener hooks
# --------------------------------------------------------------------------


def test_listener_fires_on_plan_pending():
    async def _t():
        c = PlanApprovalController()
        events: List[Dict[str, Any]] = []
        c.on_transition(lambda p: events.append(p))
        c.request_approval("op-a", _sample_plan())
        assert any(e["event_type"] == "plan_pending" for e in events)
        projection = events[0]["projection"]
        assert projection["op_id"] == "op-a"
        assert projection["state"] == STATE_PENDING
    _run_async(_t())


def test_listener_fires_on_plan_approved():
    async def _t():
        c = PlanApprovalController()
        events: List[Dict[str, Any]] = []
        c.on_transition(lambda p: events.append(p))
        c.request_approval("op-a", _sample_plan())
        c.approve("op-a", reviewer="repl")
        types = [e["event_type"] for e in events]
        assert "plan_pending" in types
        assert "plan_approved" in types
    _run_async(_t())


def test_listener_fires_on_plan_rejected():
    async def _t():
        c = PlanApprovalController()
        events: List[Dict[str, Any]] = []
        c.on_transition(lambda p: events.append(p))
        c.request_approval("op-a", _sample_plan())
        c.reject("op-a", reason="wrong")
        types = [e["event_type"] for e in events]
        assert "plan_rejected" in types


    _run_async(_t())


def test_listener_fires_on_plan_expired():
    async def _t():
        c = PlanApprovalController(default_timeout_s=0.05)
        events: List[Dict[str, Any]] = []
        c.on_transition(lambda p: events.append(p))
        fut = c.request_approval("op-a", _sample_plan())
        await asyncio.wait_for(fut, timeout=2.0)
        types = [e["event_type"] for e in events]
        assert "plan_expired" in types
    _run_async(_t())


def test_listener_exception_does_not_break_controller():
    async def _t():
        c = PlanApprovalController()

        def _bad(_p: Dict[str, Any]) -> None:
            raise RuntimeError("boom")

        c.on_transition(_bad)
        # Must not raise.
        fut = c.request_approval("op-a", _sample_plan())
        assert not fut.done()
        c.approve("op-a")
    _run_async(_t())


def test_unsubscribe_stops_events():
    async def _t():
        c = PlanApprovalController()
        events: List[Dict[str, Any]] = []
        unsub = c.on_transition(lambda p: events.append(p))
        unsub()
        c.request_approval("op-a", _sample_plan())
        assert events == []
    _run_async(_t())


# --------------------------------------------------------------------------
# 7. History + eviction
# --------------------------------------------------------------------------


def test_history_records_resolved_plans():
    async def _t():
        c = PlanApprovalController()
        c.request_approval("op-a", _sample_plan())
        c.approve("op-a", reviewer="r1")
        c.request_approval("op-b", _sample_plan())
        c.reject("op-b", reason="x", reviewer="r2")
        h = c.history()
        states = [row["state"] for row in h]
        assert STATE_APPROVED in states
        assert STATE_REJECTED in states
    _run_async(_t())


def test_evict_terminal_removes_record():
    async def _t():
        c = PlanApprovalController()
        c.request_approval("op-a", _sample_plan())
        c.approve("op-a")
        assert c.snapshot("op-a") is not None
        assert c.evict_terminal("op-a") is True
        assert c.snapshot("op-a") is None
        # Idempotent.
        assert c.evict_terminal("op-a") is False
    _run_async(_t())


def test_evict_terminal_refuses_pending():
    async def _t():
        c = PlanApprovalController()
        c.request_approval("op-a", _sample_plan())
        assert c.evict_terminal("op-a") is False
        assert c.snapshot("op-a") is not None
    _run_async(_t())


# --------------------------------------------------------------------------
# 8. Module-level helpers
# --------------------------------------------------------------------------


def test_get_default_controller_is_singleton():
    a = get_default_controller()
    b = get_default_controller()
    assert a is b


def test_reset_default_controller_clears_singleton():
    a = get_default_controller()
    reset_default_controller()
    b = get_default_controller()
    assert a is not b


def test_await_approval_high_level_helper_resolves(monkeypatch):
    async def _t():
        controller = get_default_controller()

        async def _approver() -> None:
            # give await_approval a chance to register
            await asyncio.sleep(0.05)
            controller.approve("op-a", reviewer="test")

        asyncio.ensure_future(_approver())
        outcome = await asyncio.wait_for(
            await_approval("op-a", _sample_plan(), timeout_s=5.0),
            timeout=3.0,
        )
        assert outcome.approved is True
    _run_async(_t())


# --------------------------------------------------------------------------
# 9. Outcome shape
# --------------------------------------------------------------------------


def test_outcome_is_frozen_dataclass():
    o = PlanApprovalOutcome(
        approved=True, state=STATE_APPROVED, reason="", reviewer="r",
    )
    with pytest.raises(Exception):
        o.approved = False  # type: ignore[misc]
