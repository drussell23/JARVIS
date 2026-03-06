"""Tests for StateOracle protocol and MockStateOracle implementation.

TDD tests for Task 3 of the Disease 9 cross-repo integration test harness.
asyncio_mode = auto in pytest.ini -- no @pytest.mark.asyncio required.
"""

from __future__ import annotations

import asyncio
from typing import List

import pytest

from tests.harness.state_oracle import (
    MockStateOracle,
    OracleDivergenceError,
    OracleTimeoutError,
    StateOracleProtocol,
)
from tests.harness.types import (
    ComponentStatus,
    ContractReasonCode,
    ContractStatus,
    ObservedEvent,
    OracleObservation,
)


# ---------------------------------------------------------------------------
# TestMockStateOracleBasics (10 tests)
# ---------------------------------------------------------------------------
class TestMockStateOracleBasics:
    """Basic get/set and event emission for MockStateOracle."""

    def test_initial_component_status_unknown(self) -> None:
        oracle = MockStateOracle()
        obs = oracle.component_status("voice")
        assert obs.value == ComponentStatus.UNKNOWN

    def test_set_and_get_component_status(self) -> None:
        oracle = MockStateOracle()
        oracle.set_component_status("voice", ComponentStatus.READY)
        obs = oracle.component_status("voice")
        assert obs.value == ComponentStatus.READY
        assert obs.observation_quality == "fresh"
        assert obs.source == "mock_oracle"

    def test_set_component_emits_event(self) -> None:
        oracle = MockStateOracle()
        oracle.set_component_status("voice", ComponentStatus.READY)
        events = oracle.event_log()
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "state_change"
        assert ev.component == "voice"
        assert ev.old_value == ComponentStatus.UNKNOWN.value
        assert ev.new_value == ComponentStatus.READY.value

    def test_event_seq_monotonic(self) -> None:
        oracle = MockStateOracle()
        oracle.set_component_status("voice", ComponentStatus.READY)
        oracle.set_component_status("tts", ComponentStatus.STARTING)
        oracle.set_component_status("voice", ComponentStatus.DEGRADED)
        events = oracle.event_log()
        seqs = [ev.oracle_event_seq for ev in events]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs), "Sequences must be unique"
        assert all(s > 0 for s in seqs), "Sequences must be positive"

    def test_routing_decision(self) -> None:
        oracle = MockStateOracle()
        oracle.set_routing_decision("PRIME_API")
        obs = oracle.routing_decision()
        assert obs.value == "PRIME_API"
        assert obs.observation_quality == "fresh"

    def test_epoch(self) -> None:
        oracle = MockStateOracle()
        assert oracle.epoch() == 0
        oracle.set_epoch(5)
        assert oracle.epoch() == 5

    def test_contract_status(self) -> None:
        oracle = MockStateOracle()
        cs = ContractStatus(
            compatible=True,
            reason_code=ContractReasonCode.OK,
        )
        oracle.set_contract_status("voice_v2", cs)
        result = oracle.contract_status("voice_v2")
        assert result.compatible is True
        assert result.reason_code == ContractReasonCode.OK

    def test_store_revision(self) -> None:
        oracle = MockStateOracle()
        assert oracle.store_revision("memory") == 0
        oracle.set_store_revision("memory", 42)
        assert oracle.store_revision("memory") == 42

    def test_event_log_since_phase(self) -> None:
        oracle = MockStateOracle()
        # Events during "setup" phase
        oracle.set_component_status("voice", ComponentStatus.READY)
        oracle.set_component_status("tts", ComponentStatus.READY)

        # Fence to "inject" phase
        oracle.fence_phase("inject", oracle.current_seq())

        # Events during "inject" phase
        oracle.set_component_status("voice", ComponentStatus.DEGRADED)

        # Filter: only events since "inject"
        inject_events = oracle.event_log(since_phase="inject")
        assert len(inject_events) == 1
        assert inject_events[0].component == "voice"
        assert inject_events[0].new_value == ComponentStatus.DEGRADED.value

    def test_current_seq(self) -> None:
        oracle = MockStateOracle()
        assert oracle.current_seq() == 0
        oracle.set_component_status("voice", ComponentStatus.READY)
        assert oracle.current_seq() > 0


# ---------------------------------------------------------------------------
# TestMockStateOracleWaitUntil (3 tests)
# ---------------------------------------------------------------------------
class TestMockStateOracleWaitUntil:
    """Async wait_until with predicate, deadline, and timeout."""

    async def test_wait_until_already_true(self) -> None:
        oracle = MockStateOracle()
        oracle.set_component_status("voice", ComponentStatus.READY)

        await oracle.wait_until(
            predicate=lambda: oracle.component_status("voice").value == ComponentStatus.READY,
            deadline=1.0,
            description="voice should be READY",
        )
        # No exception means success

    async def test_wait_until_becomes_true(self) -> None:
        oracle = MockStateOracle()

        async def _set_after_delay() -> None:
            await asyncio.sleep(0.1)
            oracle.set_component_status("voice", ComponentStatus.READY)

        task = asyncio.create_task(_set_after_delay())

        await oracle.wait_until(
            predicate=lambda: oracle.component_status("voice").value == ComponentStatus.READY,
            deadline=2.0,
            description="voice should become READY",
        )
        await task  # clean up

    async def test_wait_until_timeout(self) -> None:
        oracle = MockStateOracle()

        with pytest.raises(OracleTimeoutError):
            await oracle.wait_until(
                predicate=lambda: False,
                deadline=0.2,
                description="never-true predicate",
            )


# ---------------------------------------------------------------------------
# TestMockStateOraclePhaseFencing (1 test)
# ---------------------------------------------------------------------------
class TestMockStateOraclePhaseFencing:
    """Phase fencing excludes stale events from filtered queries."""

    def test_fence_excludes_stale_events(self) -> None:
        oracle = MockStateOracle()

        # Pre-fence events
        oracle.set_component_status("a", ComponentStatus.READY)
        oracle.set_component_status("b", ComponentStatus.READY)
        boundary = oracle.current_seq()

        # Fence
        oracle.fence_phase("converge", boundary)

        # Post-fence events
        oracle.set_component_status("a", ComponentStatus.DEGRADED)

        converge_events = oracle.event_log(since_phase="converge")
        assert len(converge_events) == 1
        assert converge_events[0].new_value == ComponentStatus.DEGRADED.value

        # All events still available without filter
        all_events = oracle.event_log()
        assert len(all_events) == 3


# ---------------------------------------------------------------------------
# TestMockStateOracleEmitEvent (1 test)
# ---------------------------------------------------------------------------
class TestMockStateOracleEmitEvent:
    """External callers can emit events; oracle assigns the sequence number."""

    def test_emit_assigns_seq(self) -> None:
        oracle = MockStateOracle()
        seq = oracle.emit_event(
            source="test_injector",
            event_type="fault_injected",
            component="voice",
            old_value=None,
            new_value="FAILED",
            trace_root_id="root-abc",
            trace_id="trace-001",
            metadata={"fault_id": "f1"},
        )
        assert seq > 0
        events = oracle.event_log()
        assert len(events) == 1
        assert events[0].oracle_event_seq == seq
        assert events[0].source == "test_injector"
        assert events[0].event_type == "fault_injected"


# ---------------------------------------------------------------------------
# TestStateOracleProtocol (1 test)
# ---------------------------------------------------------------------------
class TestStateOracleProtocol:
    """MockStateOracle satisfies the StateOracleProtocol."""

    def test_mock_satisfies_protocol(self) -> None:
        oracle = MockStateOracle()
        assert isinstance(oracle, StateOracleProtocol)
