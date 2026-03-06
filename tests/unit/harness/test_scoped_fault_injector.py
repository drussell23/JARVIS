"""Tests for ScopedFaultInjector with scope boundaries and re-entrant guards.

TDD tests for Task 5 of the Disease 9 cross-repo integration test harness.
asyncio_mode = auto in pytest.ini -- no @pytest.mark.asyncio required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.harness.scoped_fault_injector import (
    FaultIsolationError,
    ReentrantFaultError,
    ScopedFaultInjector,
)
from tests.harness.state_oracle import MockStateOracle
from tests.harness.types import (
    ComponentStatus,
    FaultComposition,
    FaultHandle,
    FaultScope,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _MockInnerResult:
    """Mimics the object returned by inner.inject_failure()."""

    def __init__(self) -> None:
        self.revert = AsyncMock()


def _make_oracle() -> MockStateOracle:
    """Return a MockStateOracle with prime/backend/trinity all READY, phase='inject'."""
    oracle = MockStateOracle()
    oracle.set_component_status("prime", ComponentStatus.READY)
    oracle.set_component_status("backend", ComponentStatus.READY)
    oracle.set_component_status("trinity", ComponentStatus.READY)
    oracle.set_phase("inject")
    return oracle


def _make_inner() -> tuple[MagicMock, _MockInnerResult]:
    """Return (inner_injector_mock, inner_result) with inject_failure wired up."""
    result = _MockInnerResult()
    inner = MagicMock()
    inner.inject_failure = AsyncMock(return_value=result)
    return inner, result


# ---------------------------------------------------------------------------
# TestScopedFaultInjectorBasics (3 tests)
# ---------------------------------------------------------------------------

class TestScopedFaultInjectorBasics:
    """Inject returns a handle, emits an event, and delegates to inner."""

    async def test_inject_returns_handle(self) -> None:
        oracle = _make_oracle()
        inner, _result = _make_inner()
        injector = ScopedFaultInjector(inner=inner, oracle=oracle)

        handle = await injector.inject(
            scope=FaultScope.COMPONENT,
            target="prime",
            fault_type="crash",
            affected=frozenset({"prime"}),
            unaffected=frozenset({"backend", "trinity"}),
        )

        assert isinstance(handle, FaultHandle)
        assert handle.target == "prime"
        assert handle.scope == FaultScope.COMPONENT
        assert handle.affected_components == frozenset({"prime"})
        assert handle.unaffected_components == frozenset({"backend", "trinity"})
        # pre_fault_baseline should capture READY for prime
        assert handle.pre_fault_baseline == {"prime": "READY"}

    async def test_inject_emits_event(self) -> None:
        oracle = _make_oracle()
        inner, _result = _make_inner()
        injector = ScopedFaultInjector(inner=inner, oracle=oracle)

        handle = await injector.inject(
            scope=FaultScope.COMPONENT,
            target="prime",
            fault_type="crash",
            affected=frozenset({"prime"}),
            unaffected=frozenset({"backend", "trinity"}),
        )

        events = oracle.event_log()
        fault_events = [e for e in events if e.event_type == "fault_injected"]
        assert len(fault_events) == 1
        ev = fault_events[0]
        assert ev.source == "scoped_fault_injector"
        assert ev.component == "prime"
        assert ev.metadata.get("fault_id") == handle.fault_id

    async def test_inject_delegates_to_inner(self) -> None:
        oracle = _make_oracle()
        inner, _result = _make_inner()
        injector = ScopedFaultInjector(inner=inner, oracle=oracle)

        await injector.inject(
            scope=FaultScope.COMPONENT,
            target="prime",
            fault_type="crash",
            affected=frozenset({"prime"}),
            unaffected=frozenset({"backend", "trinity"}),
        )

        inner.inject_failure.assert_awaited_once_with("prime", "crash")


# ---------------------------------------------------------------------------
# TestScopedFaultInjectorReentrant (2 tests)
# ---------------------------------------------------------------------------

class TestScopedFaultInjectorReentrant:
    """Re-entrant guard: REJECT raises, REPLACE reverts existing."""

    async def test_reject_duplicate_fault(self) -> None:
        oracle = _make_oracle()
        inner, _result = _make_inner()
        injector = ScopedFaultInjector(inner=inner, oracle=oracle)

        await injector.inject(
            scope=FaultScope.COMPONENT,
            target="prime",
            fault_type="crash",
            affected=frozenset({"prime"}),
            unaffected=frozenset({"backend", "trinity"}),
            composition=FaultComposition.REJECT,
        )

        with pytest.raises(ReentrantFaultError):
            await injector.inject(
                scope=FaultScope.COMPONENT,
                target="prime",
                fault_type="crash",
                affected=frozenset({"prime"}),
                unaffected=frozenset({"backend", "trinity"}),
                composition=FaultComposition.REJECT,
            )

    async def test_replace_reverts_existing(self) -> None:
        oracle = _make_oracle()
        inner, result1 = _make_inner()
        injector = ScopedFaultInjector(inner=inner, oracle=oracle)

        handle1 = await injector.inject(
            scope=FaultScope.COMPONENT,
            target="prime",
            fault_type="crash",
            affected=frozenset({"prime"}),
            unaffected=frozenset({"backend", "trinity"}),
            composition=FaultComposition.REPLACE,
        )
        first_fault_id = handle1.fault_id

        # Second inject with REPLACE should succeed with a different fault_id
        handle2 = await injector.inject(
            scope=FaultScope.COMPONENT,
            target="prime",
            fault_type="crash",
            affected=frozenset({"prime"}),
            unaffected=frozenset({"backend", "trinity"}),
            composition=FaultComposition.REPLACE,
        )

        assert handle2.fault_id != first_fault_id
        # The first inner result's revert should have been called
        result1.revert.assert_awaited_once()


# ---------------------------------------------------------------------------
# TestScopedFaultInjectorRevert (2 tests)
# ---------------------------------------------------------------------------

class TestScopedFaultInjectorRevert:
    """Revert calls inner revert and clears active faults."""

    async def test_revert_calls_inner_revert(self) -> None:
        oracle = _make_oracle()
        inner, result = _make_inner()
        injector = ScopedFaultInjector(inner=inner, oracle=oracle)

        handle = await injector.inject(
            scope=FaultScope.COMPONENT,
            target="prime",
            fault_type="crash",
            affected=frozenset({"prime"}),
            unaffected=frozenset({"backend", "trinity"}),
        )

        await injector.revert(handle)

        result.revert.assert_awaited_once()

    async def test_revert_clears_active(self) -> None:
        oracle = _make_oracle()
        inner, _result = _make_inner()
        injector = ScopedFaultInjector(inner=inner, oracle=oracle)

        handle = await injector.inject(
            scope=FaultScope.COMPONENT,
            target="prime",
            fault_type="crash",
            affected=frozenset({"prime"}),
            unaffected=frozenset({"backend", "trinity"}),
            composition=FaultComposition.REJECT,
        )

        assert "prime" in injector.active_faults

        await injector.revert(handle)

        assert "prime" not in injector.active_faults

        # Should be able to inject again after revert (no ReentrantFaultError)
        handle2 = await injector.inject(
            scope=FaultScope.COMPONENT,
            target="prime",
            fault_type="crash",
            affected=frozenset({"prime"}),
            unaffected=frozenset({"backend", "trinity"}),
            composition=FaultComposition.REJECT,
        )
        assert isinstance(handle2, FaultHandle)
