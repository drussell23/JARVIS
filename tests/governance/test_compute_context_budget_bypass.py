"""Task CR1 -- Financial Context Decoupling.

Proves the hard wallet gate ``session_budget_authority.check_preflight``
EXPLICITLY allows requests tagged ``compute_context="Local_Open_Source"``
(free local open-source compute, $0.00) while leaving billed cloud providers
(Claude, DoubleWord) byte-identical when the tag is absent or different.

The scenario is the exact soak failure mode: remaining session budget is
$0.00, a $0.10 estimate is requested. For billed cloud this MUST refuse; for
free local compute it MUST pass.
"""

from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import session_budget_authority as sba
from backend.core.ouroboros.governance.session_budget_authority import (
    LOCAL_OPEN_SOURCE,
    SessionBudgetPreflightRefused,
    check_preflight,
    set_session_budget_provider,
)
from backend.core.ouroboros.governance.op_context import OperationContext


class _FakeBudgetProvider:
    """Minimal duck-typed session-budget provider exposing ``.remaining``.

    Mirrors how ``battle_test/cost_tracker.py``'s ``CostTracker`` is registered
    via ``set_session_budget_provider`` -- ``get_session_remaining_usd`` resolves
    the registered provider's numeric ``.remaining`` property.
    """

    def __init__(self, remaining: float) -> None:
        self.remaining = remaining


@pytest.fixture()
def zero_budget_provider():
    """Register a $0.00-remaining provider; clear it again in teardown so
    other tests in the process are unaffected."""
    set_session_budget_provider(_FakeBudgetProvider(remaining=0.0))
    try:
        yield
    finally:
        set_session_budget_provider(None)


def test_local_open_source_bypasses_refusal(zero_budget_provider):
    """The exact soak scenario: $0.00 remaining, $0.10 estimate, but tagged
    free local open-source compute -> ALLOWED (returns None, no raise)."""
    result = check_preflight(
        provider_name="doubleword",
        estimated_cost_usd=0.10,
        compute_context="Local_Open_Source",
        op_id="op-1",
    )
    assert result is None


def test_billed_still_refused_without_tag(zero_budget_provider):
    """$0.00 remaining, $0.10 estimate, no compute_context tag -> billed cloud
    refusal is UNCHANGED."""
    with pytest.raises(SessionBudgetPreflightRefused):
        check_preflight(
            provider_name="doubleword",
            estimated_cost_usd=0.10,
            compute_context=None,
            op_id="op-2",
        )
    # Empty-string tag (the dataclass default) is likewise NOT a bypass.
    with pytest.raises(SessionBudgetPreflightRefused):
        check_preflight(
            provider_name="doubleword",
            estimated_cost_usd=0.10,
            compute_context="",
            op_id="op-2b",
        )


def test_billed_still_refused_with_other_tag(zero_budget_provider):
    """Only the EXACT free-tier string bypasses; any other tag still refuses."""
    with pytest.raises(SessionBudgetPreflightRefused):
        check_preflight(
            provider_name="claude",
            estimated_cost_usd=0.10,
            compute_context="Billed_Cloud",
            op_id="op-3",
        )


def test_local_open_source_constant_value():
    """The module constant is the exact tag string consumers must use."""
    assert LOCAL_OPEN_SOURCE == "Local_Open_Source"


def test_operation_context_with_compute_context():
    """``OperationContext.with_compute_context`` stamps the tag via the
    dataclasses.replace ``with_*`` idiom, default empty otherwise."""
    ctx = OperationContext.create(
        op_id="op-ctx", target_files=(), description="ctx test"
    )
    assert ctx.compute_context == ""
    tagged = ctx.with_compute_context("Local_Open_Source")
    assert tagged.compute_context == "Local_Open_Source"
    # Original is unchanged (frozen dataclass -- replace returns a new instance).
    assert ctx.compute_context == ""
