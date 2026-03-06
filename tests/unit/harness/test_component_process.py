"""Tests for ComponentProcess ABC and MockComponentProcess lifecycle transitions.

TDD tests for Task 7 of the Disease 9 cross-repo integration test harness.
asyncio_mode = auto in pytest.ini -- no @pytest.mark.asyncio required.
"""

from __future__ import annotations

from tests.harness.component_process import MockComponentProcess
from tests.harness.state_oracle import MockStateOracle
from tests.harness.types import ComponentStatus


# ---------------------------------------------------------------------------
# TestMockComponentProcess (5 tests)
# ---------------------------------------------------------------------------

class TestMockComponentProcess:
    """MockComponentProcess lifecycle transitions."""

    async def test_start_transitions_to_ready(self) -> None:
        oracle = MockStateOracle()
        proc = MockComponentProcess(name="prime", oracle=oracle)

        await proc.start()

        obs = oracle.component_status("prime")
        assert obs.value == ComponentStatus.READY

    async def test_stop_transitions_to_stopped(self) -> None:
        oracle = MockStateOracle()
        proc = MockComponentProcess(name="prime", oracle=oracle)

        await proc.start()
        await proc.stop()

        obs = oracle.component_status("prime")
        assert obs.value == ComponentStatus.STOPPED

    async def test_kill_transitions_to_failed(self) -> None:
        oracle = MockStateOracle()
        proc = MockComponentProcess(name="prime", oracle=oracle)

        await proc.start()
        await proc.kill()

        obs = oracle.component_status("prime")
        assert obs.value == ComponentStatus.FAILED

    async def test_start_after_kill_recovers(self) -> None:
        oracle = MockStateOracle()
        proc = MockComponentProcess(name="prime", oracle=oracle)

        await proc.start()
        await proc.kill()

        # After kill, start again should recover to READY
        await proc.start()

        obs = oracle.component_status("prime")
        assert obs.value == ComponentStatus.READY

    async def test_initial_status_registered(self) -> None:
        oracle = MockStateOracle()
        proc = MockComponentProcess(name="backend", oracle=oracle)

        # Before start, status should be UNKNOWN
        obs = oracle.component_status("backend")
        assert obs.value == ComponentStatus.UNKNOWN
