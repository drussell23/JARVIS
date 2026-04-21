"""Regression spine — PlanApproval Slice 2: adapter + force-review helper.

Covers:

  1. should_force_plan_review() mirrors the env-flag semantics so
     orchestrator can OR it into its _should_gate predicate to
     engage on EVERY op when plan mode is on.
  2. PlanApprovalProviderAdapter implements the ApprovalProvider
     surface that orchestrator already uses (request_plan /
     approve / reject / await_decision).
  3. Idempotent request_plan on same op_id.
  4. request_plan rejects contexts missing op_id.
  5. approve / reject / timeout propagate through to
     ApprovalStatus.APPROVED / REJECTED / EXPIRED.
  6. await_decision on unknown request_id returns EXPIRED
     without hanging.
  7. Adapter delegates to the shared default controller by default,
     so REPL/IDE observers see the same plans.
  8. controller.await_outcome helper returns the same outcome as
     the internal Future.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from backend.core.ouroboros.governance import plan_approval
from backend.core.ouroboros.governance.plan_approval import (
    PlanApprovalController,
    PlanApprovalOutcome,
    PlanApprovalProviderAdapter,
    PlanApprovalStateError,
    STATE_APPROVED,
    STATE_EXPIRED,
    STATE_PENDING,
    STATE_REJECTED,
    get_default_controller,
    needs_approval,
    plan_approval_mode_enabled,
    reset_default_controller,
    should_force_plan_review,
)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


_ENV_KEYS = [
    "JARVIS_PLAN_APPROVAL_MODE",
    "JARVIS_PLAN_APPROVAL_TIMEOUT_S",
    "JARVIS_PLAN_APPROVAL_MAX_PENDING",
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


def _ctx(op_id: str, description: str = "", target_files=None) -> Any:
    return SimpleNamespace(
        op_id=op_id, description=description,
        target_files=target_files or [],
    )


# --------------------------------------------------------------------------
# 1. should_force_plan_review()
# --------------------------------------------------------------------------


def test_should_force_plan_review_false_by_default():
    assert should_force_plan_review() is False


def test_should_force_plan_review_tracks_env_flag(monkeypatch):
    monkeypatch.setenv("JARVIS_PLAN_APPROVAL_MODE", "true")
    assert should_force_plan_review() is True
    assert plan_approval_mode_enabled() is True


def test_should_force_plan_review_per_op_override_false(monkeypatch):
    monkeypatch.setenv("JARVIS_PLAN_APPROVAL_MODE", "true")
    ctx = SimpleNamespace(plan_approval_override=False)
    assert should_force_plan_review(ctx) is False


def test_should_force_plan_review_per_op_override_true(monkeypatch):
    monkeypatch.setenv("JARVIS_PLAN_APPROVAL_MODE", "false")
    ctx = SimpleNamespace(plan_approval_override=True)
    assert should_force_plan_review(ctx) is True


# --------------------------------------------------------------------------
# 2. Adapter — request_plan
# --------------------------------------------------------------------------


def test_adapter_request_plan_registers_pending():
    async def _t():
        c = PlanApprovalController()
        adapter = PlanApprovalProviderAdapter(controller=c)
        rid = await adapter.request_plan(
            _ctx("op-a", description="x"), plan_text="# plan md",
        )
        assert rid == "op-a::plan"
        snap = c.snapshot("op-a")
        assert snap is not None
        assert snap["state"] == STATE_PENDING
        assert snap["plan"]["markdown"] == "# plan md"
        assert snap["plan"]["description"] == "x"
    _run_async(_t())


def test_adapter_request_plan_is_idempotent():
    async def _t():
        c = PlanApprovalController()
        adapter = PlanApprovalProviderAdapter(controller=c)
        r1 = await adapter.request_plan(_ctx("op-a"), plan_text="v1")
        r2 = await adapter.request_plan(_ctx("op-a"), plan_text="v2")
        assert r1 == r2 == "op-a::plan"
        # First plan_text wins (same as InMemoryApprovalProvider).
        snap = c.snapshot("op-a")
        assert snap["plan"]["markdown"] == "v1"
    _run_async(_t())


def test_adapter_request_plan_rejects_missing_op_id():
    async def _t():
        c = PlanApprovalController()
        adapter = PlanApprovalProviderAdapter(controller=c)
        with pytest.raises(PlanApprovalStateError):
            await adapter.request_plan(
                SimpleNamespace(op_id=""), plan_text="x",
            )
        with pytest.raises(PlanApprovalStateError):
            await adapter.request_plan(
                SimpleNamespace(op_id=None), plan_text="x",
            )
    _run_async(_t())


def test_adapter_is_plan_request_detects_suffix():
    assert PlanApprovalProviderAdapter.is_plan_request("op-a::plan") is True
    assert PlanApprovalProviderAdapter.is_plan_request("op-a") is False


# --------------------------------------------------------------------------
# 3. Adapter — approve / reject / await_decision
# --------------------------------------------------------------------------


def test_adapter_approve_and_await_decision_returns_APPROVED():
    from backend.core.ouroboros.governance.approval_provider import (
        ApprovalStatus,
    )

    async def _t():
        c = PlanApprovalController()
        adapter = PlanApprovalProviderAdapter(controller=c)
        rid = await adapter.request_plan(_ctx("op-a"), plan_text="x")

        async def _approver() -> None:
            await asyncio.sleep(0.02)
            await adapter.approve(rid, approver="repl")

        asyncio.ensure_future(_approver())
        result = await adapter.await_decision(rid, timeout_s=3.0)
        assert result.status == ApprovalStatus.APPROVED
        assert result.approver == "repl"
        assert result.request_id == rid
    _run_async(_t())


def test_adapter_reject_and_await_decision_returns_REJECTED():
    from backend.core.ouroboros.governance.approval_provider import (
        ApprovalStatus,
    )

    async def _t():
        c = PlanApprovalController()
        adapter = PlanApprovalProviderAdapter(controller=c)
        rid = await adapter.request_plan(_ctx("op-a"), plan_text="x")

        async def _rejecter() -> None:
            await asyncio.sleep(0.02)
            await adapter.reject(rid, approver="repl", reason="nope")

        asyncio.ensure_future(_rejecter())
        result = await adapter.await_decision(rid, timeout_s=3.0)
        assert result.status == ApprovalStatus.REJECTED
        assert result.reason == "nope"
    _run_async(_t())


def test_adapter_timeout_returns_EXPIRED():
    from backend.core.ouroboros.governance.approval_provider import (
        ApprovalStatus,
    )

    async def _t():
        # default_timeout_s short so the controller auto-expires.
        c = PlanApprovalController(default_timeout_s=0.05)
        adapter = PlanApprovalProviderAdapter(controller=c)
        rid = await adapter.request_plan(_ctx("op-a"), plan_text="x")
        result = await adapter.await_decision(rid, timeout_s=2.0)
        assert result.status == ApprovalStatus.EXPIRED
    _run_async(_t())


def test_adapter_await_decision_unknown_returns_EXPIRED():
    from backend.core.ouroboros.governance.approval_provider import (
        ApprovalStatus,
    )

    async def _t():
        c = PlanApprovalController()
        adapter = PlanApprovalProviderAdapter(controller=c)
        # Never called request_plan. await_decision must not hang.
        result = await adapter.await_decision(
            "never-seen::plan", timeout_s=1.0,
        )
        assert result.status == ApprovalStatus.EXPIRED
        assert result.reason == "unknown_request_id"
    _run_async(_t())


def test_adapter_await_decision_timeout_on_uncancelled_pending():
    """If someone else created the pending plan without a short
    controller timeout, a caller's await_decision(timeout_s=0.1)
    should still return EXPIRED instead of blocking forever."""
    from backend.core.ouroboros.governance.approval_provider import (
        ApprovalStatus,
    )

    async def _t():
        c = PlanApprovalController(default_timeout_s=60.0)
        adapter = PlanApprovalProviderAdapter(controller=c)
        rid = await adapter.request_plan(_ctx("op-a"), plan_text="x")
        result = await adapter.await_decision(rid, timeout_s=0.1)
        assert result.status == ApprovalStatus.EXPIRED
    _run_async(_t())


# --------------------------------------------------------------------------
# 4. Default-controller sharing
# --------------------------------------------------------------------------


def test_adapter_defaults_to_shared_default_controller(monkeypatch):
    """An adapter without an explicit controller uses the singleton —
    so REPL + IDE observers (future slices) see the same plans that
    orchestrator registers."""
    async def _t():
        reset_default_controller()
        default = get_default_controller()
        adapter = PlanApprovalProviderAdapter()
        await adapter.request_plan(_ctx("op-a"), plan_text="x")
        # Default controller sees the pending plan.
        assert default.snapshot("op-a") is not None
    _run_async(_t())


# --------------------------------------------------------------------------
# 5. await_outcome helper
# --------------------------------------------------------------------------


def test_await_outcome_returns_terminal_outcome():
    async def _t():
        c = PlanApprovalController()
        c.request_approval("op-a", {"x": 1})

        async def _approve_soon() -> None:
            await asyncio.sleep(0.02)
            c.approve("op-a", reviewer="r")

        asyncio.ensure_future(_approve_soon())
        outcome = await c.await_outcome("op-a", timeout_s=2.0)
        assert outcome.approved is True
        assert outcome.state == STATE_APPROVED
    _run_async(_t())


def test_await_outcome_timeout_short_circuits():
    async def _t():
        c = PlanApprovalController(default_timeout_s=60.0)
        c.request_approval("op-a", {})
        outcome = await c.await_outcome("op-a", timeout_s=0.1)
        assert outcome.state == STATE_EXPIRED
        assert outcome.reason == "await_timeout"
    _run_async(_t())


def test_await_outcome_unknown_raises():
    async def _t():
        c = PlanApprovalController()
        with pytest.raises(PlanApprovalStateError):
            await c.await_outcome("nonexistent")
    _run_async(_t())
