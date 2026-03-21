# tests/governance/test_elicitation.py
"""
GAP 5: Structured mid-operation elicitation via ApprovalProvider.

Five tests — all must FAIL before implementation, PASS after.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from backend.core.ouroboros.governance.approval_provider import (
    ApprovalProvider,
    CLIApprovalProvider,
)
from backend.core.ouroboros.governance.op_context import OperationContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(op_id: str = "op-elicit-1") -> OperationContext:
    return OperationContext.create(
        op_id=op_id,
        description="test elicitation op",
        target_files=("backend/foo.py",),
    )


# ---------------------------------------------------------------------------
# Test 1 — protocol defines elicit()
# ---------------------------------------------------------------------------


def test_approval_provider_has_elicit_method():
    """ApprovalProvider protocol must declare elicit() with required parameters."""
    assert hasattr(ApprovalProvider, "elicit"), (
        "ApprovalProvider protocol must define an elicit() method"
    )
    sig = inspect.signature(ApprovalProvider.elicit)
    params = set(sig.parameters)
    assert "request_id" in params, "elicit() must have a request_id parameter"
    assert "question" in params, "elicit() must have a question parameter"
    assert "options" in params, "elicit() must have an options parameter"
    assert "timeout_s" in params, "elicit() must have a timeout_s parameter"


# ---------------------------------------------------------------------------
# Test 2 — concrete class has async elicit()
# ---------------------------------------------------------------------------


def test_cli_approval_provider_has_elicit():
    """CLIApprovalProvider must have an async elicit() method."""
    assert hasattr(CLIApprovalProvider, "elicit"), (
        "CLIApprovalProvider must implement elicit()"
    )
    fn = getattr(CLIApprovalProvider, "elicit")
    assert asyncio.iscoroutinefunction(fn), "CLIApprovalProvider.elicit() must be async"


# ---------------------------------------------------------------------------
# Test 3 — programmatic answer is returned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_elicit_returns_answer():
    """_set_elicitation_answer() provides the answer that elicit() returns."""
    provider = CLIApprovalProvider()
    ctx = _make_ctx("op-elicit-answer")
    await provider.request(ctx)

    async def _answer_shortly():
        await asyncio.sleep(0.02)
        provider._set_elicitation_answer("op-elicit-answer", "yes")

    task = asyncio.create_task(_answer_shortly())
    result = await provider.elicit(
        request_id="op-elicit-answer",
        question="Should we proceed?",
        timeout_s=2.0,
    )
    await task

    assert result == "yes", f"Expected 'yes', got {result!r}"


# ---------------------------------------------------------------------------
# Test 4 — timeout returns None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_elicit_timeout_returns_none():
    """elicit() with a very short timeout returns None when no answer arrives."""
    provider = CLIApprovalProvider()
    ctx = _make_ctx("op-elicit-timeout")
    await provider.request(ctx)

    result = await provider.elicit(
        request_id="op-elicit-timeout",
        question="Any answer?",
        timeout_s=0.05,
    )

    assert result is None, f"Expected None on timeout, got {result!r}"


# ---------------------------------------------------------------------------
# Test 5 — unknown request_id raises KeyError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_elicit_unknown_request_raises():
    """elicit() on a nonexistent request_id must raise KeyError."""
    provider = CLIApprovalProvider()

    with pytest.raises(KeyError):
        await provider.elicit(
            request_id="does-not-exist",
            question="Anything?",
            timeout_s=1.0,
        )
