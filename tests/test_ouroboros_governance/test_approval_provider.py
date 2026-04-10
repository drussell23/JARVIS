"""Tests for ApprovalProvider protocol and CLIApprovalProvider implementation.

The ApprovalProvider is the human-in-the-loop approval gate for the governed
self-programming pipeline.  When the risk engine classifies an operation as
APPROVAL_REQUIRED, the orchestrator calls ``request()`` then ``await_decision()``.
A human uses CLI commands to approve/reject.

All async tests use ``@pytest.mark.asyncio``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Tuple

import pytest

from backend.core.ouroboros.governance.approval_provider import (
    ApprovalProvider,
    ApprovalResult,
    ApprovalStatus,
    CLIApprovalProvider,
)
from backend.core.ouroboros.governance.op_context import OperationContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(
    *,
    op_id: str = "op-test-001",
    description: str = "Fix utility function",
    target_files: Tuple[str, ...] = ("backend/core/utils.py",),
) -> OperationContext:
    """Build a deterministic OperationContext for testing."""
    return OperationContext.create(
        target_files=target_files,
        description=description,
        op_id=op_id,
        _timestamp=datetime(2026, 3, 7, 12, 0, 0, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# TestApprovalStatus
# ---------------------------------------------------------------------------


class TestApprovalStatus:
    """Verify the ApprovalStatus enum members."""

    def test_all_members(self) -> None:
        expected = {"PENDING", "APPROVED", "REJECTED", "EXPIRED", "SUPERSEDED"}
        actual = {s.name for s in ApprovalStatus}
        assert actual == expected


# ---------------------------------------------------------------------------
# TestApprovalResult
# ---------------------------------------------------------------------------


class TestApprovalResult:
    """Verify ApprovalResult frozen dataclass semantics."""

    def test_creation(self) -> None:
        ts = datetime(2026, 3, 7, 12, 0, 0, tzinfo=timezone.utc)
        result = ApprovalResult(
            status=ApprovalStatus.APPROVED,
            approver="derek",
            reason="Looks good",
            decided_at=ts,
            request_id="req-123",
        )
        assert result.status is ApprovalStatus.APPROVED
        assert result.approver == "derek"
        assert result.reason == "Looks good"
        assert result.decided_at == ts
        assert result.request_id == "req-123"

    def test_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        result = ApprovalResult(
            status=ApprovalStatus.PENDING,
            approver=None,
            reason=None,
            decided_at=None,
            request_id="req-456",
        )
        with pytest.raises(FrozenInstanceError):
            result.status = ApprovalStatus.APPROVED  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TestApprovalProviderProtocol
# ---------------------------------------------------------------------------


class TestApprovalProviderProtocol:
    """Verify CLIApprovalProvider satisfies the ApprovalProvider protocol."""

    def test_is_runtime_checkable(self) -> None:
        provider = CLIApprovalProvider()
        assert isinstance(provider, ApprovalProvider)


# ---------------------------------------------------------------------------
# TestCLIApprovalProvider
# ---------------------------------------------------------------------------


class TestCLIApprovalProvider:
    """Verify CLIApprovalProvider behavioral guarantees."""

    @pytest.fixture
    def provider(self) -> CLIApprovalProvider:
        return CLIApprovalProvider()

    @pytest.fixture
    def ctx(self) -> OperationContext:
        return _make_context()

    # -- request --

    @pytest.mark.asyncio
    async def test_request_returns_request_id(
        self, provider: CLIApprovalProvider, ctx: OperationContext
    ) -> None:
        request_id = await provider.request(ctx)
        assert request_id == ctx.op_id

    @pytest.mark.asyncio
    async def test_request_idempotent(
        self, provider: CLIApprovalProvider, ctx: OperationContext
    ) -> None:
        first = await provider.request(ctx)
        second = await provider.request(ctx)
        assert first == second

    # -- approve --

    @pytest.mark.asyncio
    async def test_approve_flow(
        self, provider: CLIApprovalProvider, ctx: OperationContext
    ) -> None:
        request_id = await provider.request(ctx)
        result = await provider.approve(request_id, approver="derek")
        assert result.status is ApprovalStatus.APPROVED
        assert result.approver == "derek"
        assert result.request_id == request_id
        assert isinstance(result.decided_at, datetime)

    @pytest.mark.asyncio
    async def test_idempotent_approve(
        self, provider: CLIApprovalProvider, ctx: OperationContext
    ) -> None:
        request_id = await provider.request(ctx)
        first = await provider.approve(request_id, approver="derek")
        second = await provider.approve(request_id, approver="derek")
        assert first.status is ApprovalStatus.APPROVED
        assert second.status is ApprovalStatus.APPROVED
        # Same decision object returned
        assert first.decided_at == second.decided_at
        assert first.approver == second.approver

    # -- reject --

    @pytest.mark.asyncio
    async def test_reject_flow(
        self, provider: CLIApprovalProvider, ctx: OperationContext
    ) -> None:
        request_id = await provider.request(ctx)
        result = await provider.reject(
            request_id, approver="derek", reason="Too risky"
        )
        assert result.status is ApprovalStatus.REJECTED
        assert result.approver == "derek"
        assert result.reason == "Too risky"
        assert result.request_id == request_id
        assert isinstance(result.decided_at, datetime)

    # -- await_decision --

    @pytest.mark.asyncio
    async def test_await_decision_resolves_on_approve(
        self, provider: CLIApprovalProvider, ctx: OperationContext
    ) -> None:
        request_id = await provider.request(ctx)

        async def _approve_after_delay() -> None:
            await asyncio.sleep(0.05)
            await provider.approve(request_id, approver="derek")

        asyncio.create_task(_approve_after_delay())
        result = await provider.await_decision(request_id, timeout_s=5.0)
        assert result.status is ApprovalStatus.APPROVED
        assert result.approver == "derek"

    @pytest.mark.asyncio
    async def test_await_decision_timeout_returns_expired(
        self, provider: CLIApprovalProvider, ctx: OperationContext
    ) -> None:
        request_id = await provider.request(ctx)
        result = await provider.await_decision(request_id, timeout_s=0.05)
        assert result.status is ApprovalStatus.EXPIRED
        assert result.request_id == request_id

    # -- late decision after expired -> SUPERSEDED --

    @pytest.mark.asyncio
    async def test_late_decision_after_expired_returns_superseded(
        self, provider: CLIApprovalProvider, ctx: OperationContext
    ) -> None:
        request_id = await provider.request(ctx)
        # Let it expire
        expired = await provider.await_decision(request_id, timeout_s=0.05)
        assert expired.status is ApprovalStatus.EXPIRED
        # Now try to approve after expiry
        result = await provider.approve(request_id, approver="derek")
        assert result.status is ApprovalStatus.SUPERSEDED

    # -- unknown request_id --

    @pytest.mark.asyncio
    async def test_approve_unknown_request_raises_key_error(
        self, provider: CLIApprovalProvider
    ) -> None:
        with pytest.raises(KeyError):
            await provider.approve("nonexistent-id", approver="derek")

    @pytest.mark.asyncio
    async def test_reject_unknown_request_raises_key_error(
        self, provider: CLIApprovalProvider
    ) -> None:
        with pytest.raises(KeyError):
            await provider.reject(
                "nonexistent-id", approver="derek", reason="No"
            )

    @pytest.mark.asyncio
    async def test_await_decision_unknown_request_raises_key_error(
        self, provider: CLIApprovalProvider
    ) -> None:
        with pytest.raises(KeyError):
            await provider.await_decision("nonexistent-id", timeout_s=1.0)

    # -- list_pending --

    @pytest.mark.asyncio
    async def test_list_pending(
        self, provider: CLIApprovalProvider, ctx: OperationContext
    ) -> None:
        await provider.request(ctx)
        pending = await provider.list_pending()
        assert len(pending) == 1
        entry = pending[0]
        assert entry["op_id"] == ctx.op_id
        assert entry["description"] == ctx.description
        assert entry["target_files"] == ctx.target_files
        assert "created_at" in entry
        assert entry["request_id"] == ctx.op_id

    @pytest.mark.asyncio
    async def test_list_pending_excludes_decided(
        self, provider: CLIApprovalProvider, ctx: OperationContext
    ) -> None:
        request_id = await provider.request(ctx)
        await provider.approve(request_id, approver="derek")
        pending = await provider.list_pending()
        assert len(pending) == 0

    # -- idempotent reject --

    @pytest.mark.asyncio
    async def test_idempotent_reject(
        self, provider: CLIApprovalProvider, ctx: OperationContext
    ) -> None:
        request_id = await provider.request(ctx)
        first = await provider.reject(
            request_id, approver="derek", reason="No"
        )
        second = await provider.reject(
            request_id, approver="derek", reason="Still no"
        )
        assert first.status is ApprovalStatus.REJECTED
        assert second.status is ApprovalStatus.REJECTED
        # Same decision returned (first reason preserved)
        assert first.decided_at == second.decided_at
        assert second.reason == first.reason

    # -- late reject after expired -> SUPERSEDED --

    @pytest.mark.asyncio
    async def test_late_reject_after_expired_returns_superseded(
        self, provider: CLIApprovalProvider, ctx: OperationContext
    ) -> None:
        request_id = await provider.request(ctx)
        await provider.await_decision(request_id, timeout_s=0.05)
        result = await provider.reject(
            request_id, approver="derek", reason="Too late"
        )
        assert result.status is ApprovalStatus.SUPERSEDED

    # -- approve after reject -> SUPERSEDED --

    @pytest.mark.asyncio
    async def test_approve_after_reject_returns_superseded(
        self, provider: CLIApprovalProvider, ctx: OperationContext
    ) -> None:
        request_id = await provider.request(ctx)
        await provider.reject(request_id, approver="derek", reason="No")
        result = await provider.approve(request_id, approver="derek")
        assert result.status is ApprovalStatus.SUPERSEDED

    # -- reject after approve -> SUPERSEDED --

    @pytest.mark.asyncio
    async def test_reject_after_approve_returns_superseded(
        self, provider: CLIApprovalProvider, ctx: OperationContext
    ) -> None:
        request_id = await provider.request(ctx)
        await provider.approve(request_id, approver="derek")
        result = await provider.reject(
            request_id, approver="derek", reason="Changed mind"
        )
        assert result.status is ApprovalStatus.SUPERSEDED


# ---------------------------------------------------------------------------
# TestRequestPlan (Phase 1b: Plan Approval Hard Gate)
# ---------------------------------------------------------------------------


class TestRequestPlan:
    """Plan-variant approval flow — the gate that blocks pre-GENERATE.

    Plan approval uses a composite key ``{op_id}::plan`` so it can coexist
    with code approval for the same op. SerpentApprovalProvider checks the
    ``plan_text`` field on the pending request to decide whether to render
    a code diff or plan markdown.
    """

    @pytest.fixture
    def provider(self) -> CLIApprovalProvider:
        return CLIApprovalProvider()

    @pytest.fixture
    def ctx(self) -> OperationContext:
        return _make_context()

    def test_plan_request_id_helper(self) -> None:
        assert (
            CLIApprovalProvider._plan_request_id("op-abc")
            == "op-abc::plan"
        )

    def test_is_plan_request_positive(self) -> None:
        assert CLIApprovalProvider.is_plan_request("op-abc::plan") is True

    def test_is_plan_request_negative(self) -> None:
        assert CLIApprovalProvider.is_plan_request("op-abc") is False

    @pytest.mark.asyncio
    async def test_request_plan_returns_namespaced_id(
        self, provider: CLIApprovalProvider, ctx: OperationContext
    ) -> None:
        plan_id = await provider.request_plan(ctx, "# Plan\n- Step 1")
        assert plan_id == f"{ctx.op_id}::plan"
        assert plan_id != ctx.op_id

    @pytest.mark.asyncio
    async def test_request_plan_stores_plan_text(
        self, provider: CLIApprovalProvider, ctx: OperationContext
    ) -> None:
        plan_text = "## Approach\n- Refactor auth middleware"
        plan_id = await provider.request_plan(ctx, plan_text)
        pending = provider._requests[plan_id]
        assert pending.plan_text == plan_text

    @pytest.mark.asyncio
    async def test_code_request_has_no_plan_text(
        self, provider: CLIApprovalProvider, ctx: OperationContext
    ) -> None:
        """Regular request() must NOT set plan_text (distinguishability)."""
        code_id = await provider.request(ctx)
        assert provider._requests[code_id].plan_text is None

    @pytest.mark.asyncio
    async def test_request_plan_and_request_coexist(
        self, provider: CLIApprovalProvider, ctx: OperationContext
    ) -> None:
        """Same op can have both a plan request and a code request pending."""
        code_id = await provider.request(ctx)
        plan_id = await provider.request_plan(ctx, "# Plan")
        assert code_id != plan_id
        assert code_id in provider._requests
        assert plan_id in provider._requests
        # They are independent pending requests
        assert provider._requests[code_id].plan_text is None
        assert provider._requests[plan_id].plan_text == "# Plan"

    @pytest.mark.asyncio
    async def test_request_plan_idempotent(
        self, provider: CLIApprovalProvider, ctx: OperationContext
    ) -> None:
        first = await provider.request_plan(ctx, "# First plan")
        second = await provider.request_plan(ctx, "# Second plan (ignored)")
        assert first == second
        # First plan text is preserved (idempotent)
        assert provider._requests[first].plan_text == "# First plan"

    @pytest.mark.asyncio
    async def test_plan_approve_via_standard_approve(
        self, provider: CLIApprovalProvider, ctx: OperationContext
    ) -> None:
        """Plan requests approve via the standard approve() method,
        using the composite key."""
        plan_id = await provider.request_plan(ctx, "# Plan")
        result = await provider.approve(plan_id, approver="derek")
        assert result.status is ApprovalStatus.APPROVED
        assert result.request_id == plan_id

    @pytest.mark.asyncio
    async def test_plan_reject_via_standard_reject(
        self, provider: CLIApprovalProvider, ctx: OperationContext
    ) -> None:
        plan_id = await provider.request_plan(ctx, "# Plan")
        result = await provider.reject(
            plan_id, approver="derek", reason="Wrong approach"
        )
        assert result.status is ApprovalStatus.REJECTED
        assert result.reason == "Wrong approach"

    @pytest.mark.asyncio
    async def test_plan_await_decision_blocks_until_approved(
        self, provider: CLIApprovalProvider, ctx: OperationContext
    ) -> None:
        plan_id = await provider.request_plan(ctx, "# Plan")

        async def _approve_after_delay() -> None:
            await asyncio.sleep(0.05)
            await provider.approve(plan_id, approver="derek")

        asyncio.create_task(_approve_after_delay())
        result = await provider.await_decision(plan_id, timeout_s=2.0)
        assert result.status is ApprovalStatus.APPROVED

    @pytest.mark.asyncio
    async def test_plan_await_decision_timeout_expired(
        self, provider: CLIApprovalProvider, ctx: OperationContext
    ) -> None:
        plan_id = await provider.request_plan(ctx, "# Plan")
        result = await provider.await_decision(plan_id, timeout_s=0.05)
        assert result.status is ApprovalStatus.EXPIRED
