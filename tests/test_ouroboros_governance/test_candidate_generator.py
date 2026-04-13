"""Tests for CandidateGenerator, FailbackStateMachine, and CandidateProvider protocol.

The CandidateGenerator routes code generation requests to a primary provider
(GCP J-Prime) or a fallback provider (local model).  The FailbackStateMachine
prevents flapping by requiring N consecutive health probes over a dwell period
before restoring the primary provider.

All async tests use ``@pytest.mark.asyncio``.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Tuple
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest

from backend.core.ouroboros.governance.candidate_generator import (
    CandidateGenerator,
    CandidateProvider,
    FailbackState,
    FailbackStateMachine,
    FailureMode,
)
from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(
    *,
    op_id: str = "op-gen-001",
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


def _make_generation_result(
    provider_name: str = "test-primary",
    duration: float = 1.5,
) -> GenerationResult:
    """Build a deterministic GenerationResult for testing."""
    return GenerationResult(
        candidates=({"file": "utils.py", "diff": "+fix"},),
        provider_name=provider_name,
        generation_duration_s=duration,
    )


def _make_deadline(seconds_from_now: float = 10.0) -> datetime:
    """Build a deadline in the near future."""
    return datetime.now(tz=timezone.utc) + timedelta(seconds=seconds_from_now)


def _make_mock_provider(
    name: str = "mock-primary",
    result: GenerationResult | None = None,
    healthy: bool = True,
    generate_side_effect: Exception | None = None,
) -> AsyncMock:
    """Create a mock CandidateProvider.

    Uses AsyncMock for the async methods and PropertyMock for provider_name.
    """
    provider = AsyncMock(spec=CandidateProvider)

    # provider_name is a property, so we configure it via PropertyMock
    type(provider).provider_name = PropertyMock(return_value=name)

    if generate_side_effect is not None:
        provider.generate.side_effect = generate_side_effect
    elif result is not None:
        provider.generate.return_value = result
    else:
        provider.generate.return_value = _make_generation_result(provider_name=name)

    provider.health_probe.return_value = healthy
    return provider


# ---------------------------------------------------------------------------
# TestFailbackState
# ---------------------------------------------------------------------------


class TestFailbackState:
    """Verify the FailbackState enum members."""

    def test_all_members(self) -> None:
        expected = {"PRIMARY_READY", "FALLBACK_ACTIVE", "PRIMARY_DEGRADED", "QUEUE_ONLY"}
        actual = {s.name for s in FailbackState}
        assert actual == expected


# ---------------------------------------------------------------------------
# TestFailbackStateMachine
# ---------------------------------------------------------------------------


class TestFailbackStateMachine:
    """Verify the FailbackStateMachine transition logic.

    Key invariant: failover is immediate (one failure), but failback
    requires N probes over a dwell period.
    """

    def test_initial_state_is_primary_ready(self) -> None:
        fsm = FailbackStateMachine()
        assert fsm.state is FailbackState.PRIMARY_READY

    def test_primary_failure_transitions_to_fallback_active(self) -> None:
        fsm = FailbackStateMachine()
        fsm.record_primary_failure()
        assert fsm.state is FailbackState.FALLBACK_ACTIVE

    def test_primary_failure_from_degraded_stays_fallback(self) -> None:
        """If primary was degraded and fails again, go back to FALLBACK_ACTIVE."""
        fsm = FailbackStateMachine(required_probes=3, dwell_time_s=0.0)
        fsm.record_primary_failure()  # -> FALLBACK_ACTIVE
        fsm.record_probe_success()    # -> PRIMARY_DEGRADED (first probe)
        assert fsm.state is FailbackState.PRIMARY_DEGRADED
        fsm.record_primary_failure()  # -> FALLBACK_ACTIVE
        assert fsm.state is FailbackState.FALLBACK_ACTIVE

    def test_single_probe_not_enough_for_recovery(self) -> None:
        """One probe success should move to PRIMARY_DEGRADED but not PRIMARY_READY."""
        fsm = FailbackStateMachine(required_probes=3, dwell_time_s=0.0)
        fsm.record_primary_failure()  # -> FALLBACK_ACTIVE
        fsm.record_probe_success()    # -> PRIMARY_DEGRADED
        assert fsm.state is FailbackState.PRIMARY_DEGRADED

    def test_three_probes_with_zero_dwell_recovers(self) -> None:
        """With dwell_time_s=0, three probes should be enough to recover."""
        fsm = FailbackStateMachine(required_probes=3, dwell_time_s=0.0)
        fsm.record_primary_failure()  # -> FALLBACK_ACTIVE

        fsm.record_probe_success()    # probe 1 -> PRIMARY_DEGRADED
        assert fsm.state is FailbackState.PRIMARY_DEGRADED

        fsm.record_probe_success()    # probe 2 -> still PRIMARY_DEGRADED
        assert fsm.state is FailbackState.PRIMARY_DEGRADED

        fsm.record_probe_success()    # probe 3 -> PRIMARY_READY (dwell=0)
        assert fsm.state is FailbackState.PRIMARY_READY

    def test_dwell_time_enforced(self) -> None:
        """Even with enough probes, must wait for dwell_time_s to elapse."""
        fsm = FailbackStateMachine(required_probes=2, dwell_time_s=100.0)
        fsm.record_primary_failure()  # -> FALLBACK_ACTIVE

        fsm.record_probe_success()    # probe 1 -> PRIMARY_DEGRADED
        fsm.record_probe_success()    # probe 2 -> still PRIMARY_DEGRADED (dwell not met)
        assert fsm.state is FailbackState.PRIMARY_DEGRADED

    def test_dwell_time_satisfied_after_wait(self) -> None:
        """When dwell_time_s has elapsed AND required_probes met -> PRIMARY_READY."""
        fsm = FailbackStateMachine(required_probes=2, dwell_time_s=0.0)
        fsm.record_primary_failure()

        # Patch time.monotonic to simulate dwell period
        fsm.record_probe_success()
        fsm.record_probe_success()
        # With dwell_time_s=0.0, this should recover immediately
        assert fsm.state is FailbackState.PRIMARY_READY

    def test_probe_failure_resets_from_degraded_to_fallback(self) -> None:
        """A probe failure while PRIMARY_DEGRADED resets to FALLBACK_ACTIVE."""
        fsm = FailbackStateMachine(required_probes=3, dwell_time_s=0.0)
        fsm.record_primary_failure()  # -> FALLBACK_ACTIVE
        fsm.record_probe_success()    # -> PRIMARY_DEGRADED
        fsm.record_probe_success()    # probe 2

        fsm.record_probe_failure()    # -> FALLBACK_ACTIVE (resets)
        assert fsm.state is FailbackState.FALLBACK_ACTIVE

    def test_probe_failure_resets_probe_count(self) -> None:
        """After a probe failure, must accumulate required_probes again from zero."""
        fsm = FailbackStateMachine(required_probes=2, dwell_time_s=0.0)
        fsm.record_primary_failure()

        fsm.record_probe_success()    # probe 1
        fsm.record_probe_failure()    # reset -> FALLBACK_ACTIVE

        fsm.record_probe_success()    # probe 1 (restarted)
        assert fsm.state is FailbackState.PRIMARY_DEGRADED

        fsm.record_probe_success()    # probe 2 -> PRIMARY_READY
        assert fsm.state is FailbackState.PRIMARY_READY

    def test_fallback_transient_failure_stays_fallback_active(self) -> None:
        """Transient fallback failures (TIMEOUT) don't go to QUEUE_ONLY."""
        fsm = FailbackStateMachine()
        fsm.record_primary_failure()   # -> FALLBACK_ACTIVE
        fsm.record_fallback_failure(mode=FailureMode.TIMEOUT)
        assert fsm.state is FailbackState.FALLBACK_ACTIVE

    def test_fallback_permanent_failure_transitions_to_queue_only(self) -> None:
        """Permanent fallback failures (CONNECTION_ERROR) go to QUEUE_ONLY."""
        fsm = FailbackStateMachine()
        fsm.record_primary_failure()   # -> FALLBACK_ACTIVE
        fsm.record_fallback_failure(mode=FailureMode.CONNECTION_ERROR)
        assert fsm.state is FailbackState.QUEUE_ONLY

    def test_probe_success_from_queue_only_auto_recovers(self) -> None:
        """QUEUE_ONLY auto-recovers to FALLBACK_ACTIVE on probe success.

        When a health probe succeeds, the primary is alive again and the
        system should exit the dead-end state to resume generation.
        """
        fsm = FailbackStateMachine()
        fsm.record_primary_failure()
        fsm.record_fallback_failure(mode=FailureMode.CONNECTION_ERROR)
        assert fsm.state is FailbackState.QUEUE_ONLY

        # Probe success should pull us out of QUEUE_ONLY.
        # The probe transitions QUEUE_ONLY → FALLBACK_ACTIVE, then
        # immediately counts as first probe → PRIMARY_DEGRADED.
        fsm.record_probe_success()
        assert fsm.state is FailbackState.PRIMARY_DEGRADED

    def test_multiple_primary_failures_are_idempotent(self) -> None:
        """Repeated primary failures don't change state beyond FALLBACK_ACTIVE."""
        fsm = FailbackStateMachine()
        fsm.record_primary_failure()
        fsm.record_primary_failure()
        fsm.record_primary_failure()
        assert fsm.state is FailbackState.FALLBACK_ACTIVE

    def test_probe_success_from_primary_ready_is_noop(self) -> None:
        """Probing while already PRIMARY_READY does nothing harmful."""
        fsm = FailbackStateMachine()
        assert fsm.state is FailbackState.PRIMARY_READY
        fsm.record_probe_success()
        assert fsm.state is FailbackState.PRIMARY_READY

    def test_dwell_time_uses_monotonic_clock(self) -> None:
        """Verify that the FSM uses time.monotonic for dwell tracking."""
        fsm = FailbackStateMachine(required_probes=1, dwell_time_s=1000.0)
        fsm.record_primary_failure()

        with patch("time.monotonic", return_value=0.0):
            fsm.record_probe_success()  # first probe at t=0
        assert fsm.state is FailbackState.PRIMARY_DEGRADED

        # Time has not passed enough
        with patch("time.monotonic", return_value=999.0):
            fsm.record_probe_success()  # not enough dwell
        assert fsm.state is FailbackState.PRIMARY_DEGRADED

        # Now time is past dwell
        with patch("time.monotonic", return_value=1001.0):
            fsm.record_probe_success()
        assert fsm.state is FailbackState.PRIMARY_READY


# ---------------------------------------------------------------------------
# TestCandidateProviderProtocol
# ---------------------------------------------------------------------------


class TestCandidateProviderProtocol:
    """Verify CandidateProvider is a runtime-checkable protocol."""

    def test_mock_satisfies_protocol(self) -> None:
        provider = _make_mock_provider()
        assert isinstance(provider, CandidateProvider)


# ---------------------------------------------------------------------------
# TestCandidateGenerator
# ---------------------------------------------------------------------------


class TestCandidateGenerator:
    """Verify CandidateGenerator behavioral guarantees."""

    @pytest.fixture
    def ctx(self) -> OperationContext:
        return _make_context()

    @pytest.fixture
    def primary_result(self) -> GenerationResult:
        return _make_generation_result(provider_name="primary")

    @pytest.fixture
    def fallback_result(self) -> GenerationResult:
        return _make_generation_result(provider_name="fallback", duration=2.0)

    # -- primary success --

    @pytest.mark.asyncio
    async def test_primary_success(
        self, ctx: OperationContext, primary_result: GenerationResult
    ) -> None:
        primary = _make_mock_provider(name="primary", result=primary_result)
        fallback = _make_mock_provider(name="fallback")
        gen = CandidateGenerator(primary=primary, fallback=fallback)
        deadline = _make_deadline(10.0)

        result = await gen.generate(ctx, deadline)

        assert result.provider_name == "primary"
        assert result.candidates == primary_result.candidates
        primary.generate.assert_awaited_once()
        fallback.generate.assert_not_awaited()

    # -- primary timeout falls back --

    @pytest.mark.asyncio
    async def test_primary_timeout_falls_back_to_fallback(
        self, ctx: OperationContext, fallback_result: GenerationResult
    ) -> None:
        """When primary raises TimeoutError, generator should fall back."""
        primary = _make_mock_provider(
            name="primary",
            generate_side_effect=asyncio.TimeoutError("primary timed out"),
        )
        fallback = _make_mock_provider(name="fallback", result=fallback_result)
        gen = CandidateGenerator(primary=primary, fallback=fallback)
        deadline = _make_deadline(10.0)

        result = await gen.generate(ctx, deadline)

        assert result.provider_name == "fallback"
        assert gen.fsm.state is FailbackState.FALLBACK_ACTIVE

    # -- primary exception falls back --

    @pytest.mark.asyncio
    async def test_primary_exception_falls_back(
        self, ctx: OperationContext, fallback_result: GenerationResult
    ) -> None:
        primary = _make_mock_provider(
            name="primary",
            generate_side_effect=RuntimeError("GPU OOM"),
        )
        fallback = _make_mock_provider(name="fallback", result=fallback_result)
        gen = CandidateGenerator(primary=primary, fallback=fallback)
        deadline = _make_deadline(10.0)

        result = await gen.generate(ctx, deadline)

        assert result.provider_name == "fallback"
        assert gen.fsm.state is FailbackState.FALLBACK_ACTIVE

    # -- both fail raises --

    @pytest.mark.asyncio
    async def test_both_fail_raises_runtime_error(
        self, ctx: OperationContext
    ) -> None:
        primary = _make_mock_provider(
            name="primary",
            generate_side_effect=RuntimeError("primary down"),
        )
        fallback = _make_mock_provider(
            name="fallback",
            generate_side_effect=RuntimeError("fallback down"),
        )
        gen = CandidateGenerator(primary=primary, fallback=fallback)
        deadline = _make_deadline(10.0)

        with pytest.raises(RuntimeError, match="all_providers_exhausted"):
            await gen.generate(ctx, deadline)
        # Transient failures (RuntimeError classified as TIMEOUT) stay
        # FALLBACK_ACTIVE — only permanent failures go to QUEUE_ONLY.
        assert gen.fsm.state is FailbackState.FALLBACK_ACTIVE

    # -- QUEUE_ONLY raises immediately --

    @pytest.mark.asyncio
    async def test_queue_only_raises_immediately(
        self, ctx: OperationContext
    ) -> None:
        primary = _make_mock_provider(name="primary")
        fallback = _make_mock_provider(name="fallback")
        gen = CandidateGenerator(primary=primary, fallback=fallback)
        gen.fsm.record_primary_failure()
        gen.fsm.record_fallback_failure(mode=FailureMode.CONNECTION_ERROR)
        assert gen.fsm.state is FailbackState.QUEUE_ONLY

        deadline = _make_deadline(10.0)
        with pytest.raises(RuntimeError, match="all_providers_exhausted"):
            await gen.generate(ctx, deadline)

        # Neither provider should have been called
        primary.generate.assert_not_awaited()
        fallback.generate.assert_not_awaited()

    # -- FALLBACK_ACTIVE uses fallback directly --

    @pytest.mark.asyncio
    async def test_fallback_active_uses_fallback_directly(
        self, ctx: OperationContext, fallback_result: GenerationResult
    ) -> None:
        primary = _make_mock_provider(name="primary")
        fallback = _make_mock_provider(name="fallback", result=fallback_result)
        gen = CandidateGenerator(primary=primary, fallback=fallback)
        gen.fsm.record_primary_failure()
        assert gen.fsm.state is FailbackState.FALLBACK_ACTIVE

        deadline = _make_deadline(10.0)
        result = await gen.generate(ctx, deadline)

        assert result.provider_name == "fallback"
        primary.generate.assert_not_awaited()
        fallback.generate.assert_awaited_once()

    # -- PRIMARY_DEGRADED uses fallback directly --

    @pytest.mark.asyncio
    async def test_primary_degraded_uses_fallback(
        self, ctx: OperationContext, fallback_result: GenerationResult
    ) -> None:
        primary = _make_mock_provider(name="primary")
        fallback = _make_mock_provider(name="fallback", result=fallback_result)
        gen = CandidateGenerator(primary=primary, fallback=fallback)
        gen.fsm.record_primary_failure()
        gen.fsm.record_probe_success()  # -> PRIMARY_DEGRADED
        assert gen.fsm.state is FailbackState.PRIMARY_DEGRADED

        deadline = _make_deadline(10.0)
        result = await gen.generate(ctx, deadline)

        assert result.provider_name == "fallback"
        primary.generate.assert_not_awaited()

    # -- concurrency quota --

    @pytest.mark.asyncio
    async def test_concurrency_quota_limits_parallel_calls(
        self, ctx: OperationContext
    ) -> None:
        """Verify the semaphore limits concurrent primary calls."""
        active = 0
        max_active = 0
        lock = asyncio.Lock()

        async def _tracked_generate(*args, **kwargs):
            nonlocal active, max_active
            async with lock:
                active += 1
                if active > max_active:
                    max_active = active
            await asyncio.sleep(0.05)
            async with lock:
                active -= 1
            return _make_generation_result(provider_name="primary")

        primary = _make_mock_provider(name="primary")
        primary.generate.side_effect = _tracked_generate
        fallback = _make_mock_provider(name="fallback")

        gen = CandidateGenerator(
            primary=primary, fallback=fallback, primary_concurrency=2
        )
        deadline = _make_deadline(10.0)

        # Launch 5 concurrent calls
        tasks = [
            asyncio.create_task(gen.generate(_make_context(op_id=f"op-{i}"), deadline))
            for i in range(5)
        ]
        await asyncio.gather(*tasks)

        # Max active should not exceed the concurrency limit of 2
        assert max_active <= 2

    # -- health probe --

    @pytest.mark.asyncio
    async def test_run_health_probe_success_updates_fsm(self) -> None:
        primary = _make_mock_provider(name="primary", healthy=True)
        fallback = _make_mock_provider(name="fallback")
        gen = CandidateGenerator(primary=primary, fallback=fallback)
        gen.fsm.record_primary_failure()
        assert gen.fsm.state is FailbackState.FALLBACK_ACTIVE

        result = await gen.run_health_probe()
        assert result is True
        assert gen.fsm.state is FailbackState.PRIMARY_DEGRADED

    @pytest.mark.asyncio
    async def test_run_health_probe_failure_keeps_fallback(self) -> None:
        primary = _make_mock_provider(name="primary", healthy=False)
        fallback = _make_mock_provider(name="fallback")
        gen = CandidateGenerator(primary=primary, fallback=fallback)
        gen.fsm.record_primary_failure()
        assert gen.fsm.state is FailbackState.FALLBACK_ACTIVE

        result = await gen.run_health_probe()
        assert result is False
        assert gen.fsm.state is FailbackState.FALLBACK_ACTIVE

    @pytest.mark.asyncio
    async def test_run_health_probe_exception_treated_as_failure(self) -> None:
        primary = _make_mock_provider(name="primary")
        primary.health_probe.side_effect = ConnectionError("unreachable")
        fallback = _make_mock_provider(name="fallback")
        gen = CandidateGenerator(primary=primary, fallback=fallback)
        gen.fsm.record_primary_failure()
        gen.fsm.record_probe_success()  # -> PRIMARY_DEGRADED
        assert gen.fsm.state is FailbackState.PRIMARY_DEGRADED

        result = await gen.run_health_probe()
        assert result is False
        # Should reset to FALLBACK_ACTIVE
        assert gen.fsm.state is FailbackState.FALLBACK_ACTIVE

    # -- deadline propagation --

    @pytest.mark.asyncio
    async def test_expired_deadline_refreshes_for_fallback(
        self, ctx: OperationContext
    ) -> None:
        """An expired parent deadline should refresh for fallback, not hang.

        The orchestrator's outer wait_for is the absolute Iron Gate.
        An expired parent deadline typically means Tier 0 burned the window
        or the op queued behind _fallback_sem — the fallback still deserves
        a viable window.
        """
        received_deadline: list = []

        async def _capture(ctx_arg, deadline_arg):
            received_deadline.append(deadline_arg)
            return _make_generation_result(provider_name="fallback")

        primary = _make_mock_provider(
            name="primary", generate_side_effect=asyncio.TimeoutError()
        )
        fallback = _make_mock_provider(name="fallback")
        fallback.generate.side_effect = _capture
        gen = CandidateGenerator(primary=primary, fallback=fallback)
        past_deadline = datetime.now(tz=timezone.utc) - timedelta(seconds=1)

        result = await gen.generate(ctx, past_deadline)
        assert result.provider_name == "fallback"
        refreshed = (
            received_deadline[0] - datetime.now(tz=timezone.utc)
        ).total_seconds()
        assert refreshed > 60.0

    @pytest.mark.asyncio
    async def test_fallback_refreshes_depleted_deadline(
        self, ctx: OperationContext
    ) -> None:
        """Fallback should refresh the deadline when parent budget is depleted.

        Regression test for bt-2026-04-11-211131: Tier 0 (DW) was burning
        80-100s of a 120s parent window, leaving Claude with 20-40s — too
        short for legitimate doc-gen / patch streams. The refresh grants the
        fallback its own ``_FALLBACK_MIN_GUARANTEED_S`` (90s) window when
        the parent is depleted-but-alive.
        """
        # Primary fails fast → triggers fallback path.
        primary = _make_mock_provider(
            name="primary", generate_side_effect=RuntimeError("primary down")
        )
        # Fallback inspects the deadline it receives so we can verify the
        # refresh actually propagates downstream.
        received_deadline: list = []

        async def _fallback_capture(ctx_arg, deadline_arg):
            received_deadline.append(deadline_arg)
            return _make_generation_result(provider_name="fallback")

        fallback = _make_mock_provider(name="fallback")
        fallback.generate.side_effect = _fallback_capture

        gen = CandidateGenerator(primary=primary, fallback=fallback)
        # Give the parent a tiny window: 5s. Fallback should still get 90s.
        depleted_deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=5)

        result = await gen.generate(ctx, depleted_deadline)
        assert result.provider_name == "fallback"
        assert len(received_deadline) == 1
        # The fallback should have received a refreshed deadline at least
        # 60s into the future (much greater than the 5s parent window).
        refreshed_remaining = (
            received_deadline[0] - datetime.now(tz=timezone.utc)
        ).total_seconds()
        assert refreshed_remaining > 60.0, (
            f"Fallback deadline not refreshed: only {refreshed_remaining:.1f}s remaining"
        )

    @pytest.mark.asyncio
    async def test_fallback_does_not_refresh_when_parent_healthy(
        self, ctx: OperationContext
    ) -> None:
        """When the parent deadline has plenty of headroom, no refresh fires."""
        primary = _make_mock_provider(
            name="primary", generate_side_effect=RuntimeError("primary down")
        )
        received_deadline: list = []

        async def _fallback_capture(ctx_arg, deadline_arg):
            received_deadline.append(deadline_arg)
            return _make_generation_result(provider_name="fallback")

        fallback = _make_mock_provider(name="fallback")
        fallback.generate.side_effect = _fallback_capture

        gen = CandidateGenerator(primary=primary, fallback=fallback)
        # Healthy parent: 100s window. The fallback should receive a deadline
        # in roughly the same range (NOT bumped to 90 or any other constant).
        healthy_deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=100)

        await gen.generate(ctx, healthy_deadline)
        assert len(received_deadline) == 1
        # When healthy, the original deadline should pass through unchanged.
        delta_from_original = abs(
            (received_deadline[0] - healthy_deadline).total_seconds()
        )
        assert delta_from_original < 0.5, (
            f"Healthy parent deadline was unexpectedly refreshed: "
            f"delta={delta_from_original:.2f}s"
        )

    # -- fallback concurrency quota --

    @pytest.mark.asyncio
    async def test_fallback_concurrency_quota(self) -> None:
        """Verify the fallback semaphore is separate and respected."""
        active = 0
        max_active = 0
        lock = asyncio.Lock()

        async def _tracked_generate(*args, **kwargs):
            nonlocal active, max_active
            async with lock:
                active += 1
                if active > max_active:
                    max_active = active
            await asyncio.sleep(0.05)
            async with lock:
                active -= 1
            return _make_generation_result(provider_name="fallback")

        primary = _make_mock_provider(
            name="primary", generate_side_effect=RuntimeError("down")
        )
        fallback = _make_mock_provider(name="fallback")
        fallback.generate.side_effect = _tracked_generate

        gen = CandidateGenerator(
            primary=primary, fallback=fallback,
            primary_concurrency=4, fallback_concurrency=1,
        )
        # Force into FALLBACK_ACTIVE so fallback is used directly
        gen.fsm.record_primary_failure()
        deadline = _make_deadline(10.0)

        tasks = [
            asyncio.create_task(gen.generate(_make_context(op_id=f"op-{i}"), deadline))
            for i in range(4)
        ]
        await asyncio.gather(*tasks)

        assert max_active <= 1


class TestCandidateGeneratorPlan:
    async def test_plan_delegates_to_primary_when_ready(self):
        from unittest.mock import AsyncMock, MagicMock
        from datetime import datetime, timedelta, timezone
        from backend.core.ouroboros.governance.candidate_generator import CandidateGenerator

        mock_primary = MagicMock()
        mock_primary.provider_name = "primary"
        mock_primary.plan = AsyncMock(return_value='{"schema_version": "expansion.1", "additional_files_needed": [], "reasoning": "ok"}')
        mock_primary.generate = AsyncMock()
        mock_primary.health_probe = AsyncMock(return_value=True)

        mock_fallback = MagicMock()
        mock_fallback.provider_name = "fallback"
        mock_fallback.plan = AsyncMock(return_value='{}')
        mock_fallback.generate = AsyncMock()
        mock_fallback.health_probe = AsyncMock(return_value=True)

        gen = CandidateGenerator(primary=mock_primary, fallback=mock_fallback)
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)
        result = await gen.plan("test prompt", deadline)

        assert isinstance(result, str)
        mock_primary.plan.assert_called_once()
        mock_fallback.plan.assert_not_called()

    async def test_plan_falls_back_when_primary_fails(self):
        from unittest.mock import AsyncMock, MagicMock
        from datetime import datetime, timedelta, timezone
        from backend.core.ouroboros.governance.candidate_generator import CandidateGenerator

        mock_primary = MagicMock()
        mock_primary.provider_name = "primary"
        mock_primary.plan = AsyncMock(side_effect=RuntimeError("primary_down"))
        mock_primary.generate = AsyncMock()
        mock_primary.health_probe = AsyncMock(return_value=False)

        mock_fallback = MagicMock()
        mock_fallback.provider_name = "fallback"
        mock_fallback.plan = AsyncMock(return_value='{"schema_version": "expansion.1", "additional_files_needed": [], "reasoning": "fallback"}')
        mock_fallback.generate = AsyncMock()
        mock_fallback.health_probe = AsyncMock(return_value=True)

        gen = CandidateGenerator(primary=mock_primary, fallback=mock_fallback)
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)
        result = await gen.plan("test prompt", deadline)

        assert isinstance(result, str)
        mock_fallback.plan.assert_called_once()


# ---------------------------------------------------------------------------
# Task #67 — RemoteProtocolError + DW exhaustion fix
# ---------------------------------------------------------------------------
#
# Battle test bt-2026-04-12-005521 saw 9 consecutive ops die with
# `all_providers_exhausted` because the Anthropic SDK wraps httpx errors
# in `APIConnectionError(cause=RemoteProtocolError("Server disconnected ..."))`
# and the FSM classifier did not walk `__cause__` — every op fell through
# to the conservative TIMEOUT default (45s base, 300s max). The
# CONNECTION_ERROR-only deep-backoff guard never engaged, so consecutive
# ops kept hammering the dead provider.
#
# These tests cover three independent fix layers:
#  1. New TRANSIENT_TRANSPORT FailureMode with short recovery (5s/30s)
#  2. classify_exception walks __cause__/__context__ chains and routes
#     RemoteProtocolError, ClosedResourceError, ProtocolError to it.
#  3. Per-op tier rotation (CandidateGenerator._should_skip_tier0_for_op)
#     as a belt-and-suspenders guard for misclassification.


def _make_remote_protocol_error(msg: str = "Server disconnected without sending a response."):
    """Synthesize a RemoteProtocolError-named exception WITHOUT importing httpx.

    The classifier matches on class name (to avoid hard SDK imports), so a
    locally-defined class with the same name is sufficient for the test.
    """
    class RemoteProtocolError(Exception):
        pass
    return RemoteProtocolError(msg)


def _make_closed_resource_error(msg: str = ""):
    """Synthesize a ClosedResourceError-named exception."""
    class ClosedResourceError(Exception):
        pass
    return ClosedResourceError(msg)


def _make_api_connection_error_wrapping(inner: BaseException):
    """Synthesize APIConnectionError-name wrapping an inner cause.

    Mirrors the Anthropic SDK pattern where httpx exceptions are wrapped
    in APIConnectionError. The classifier should walk __cause__ to surface
    the inner exception.
    """
    class APIConnectionError(Exception):
        pass
    wrapper = APIConnectionError(f"Connection error: {type(inner).__name__}: {inner}")
    wrapper.__cause__ = inner
    return wrapper


class TestTransientTransportClassification:
    """The classifier's __cause__-walk and TRANSIENT_TRANSPORT routing."""

    def test_remote_protocol_error_classified_as_transient_transport(self):
        exc = _make_remote_protocol_error()
        mode = FailbackStateMachine.classify_exception(exc)
        assert mode is FailureMode.TRANSIENT_TRANSPORT

    def test_closed_resource_error_classified_as_transient_transport(self):
        exc = _make_closed_resource_error()
        mode = FailbackStateMachine.classify_exception(exc)
        assert mode is FailureMode.TRANSIENT_TRANSPORT

    def test_protocol_error_classified_as_transient_transport(self):
        class ProtocolError(Exception):
            pass
        mode = FailbackStateMachine.classify_exception(ProtocolError("h11 violation"))
        assert mode is FailureMode.TRANSIENT_TRANSPORT

    def test_api_connection_error_wrapping_remote_protocol_unwraps(self):
        """The actual production failure mode: SDK wrapper hides the real error.

        APIConnectionError → was being classified as 'connection' (TIMEOUT
        default). Walking __cause__ surfaces the inner RemoteProtocolError
        and routes to TRANSIENT_TRANSPORT.
        """
        inner = _make_remote_protocol_error()
        wrapper = _make_api_connection_error_wrapping(inner)
        mode = FailbackStateMachine.classify_exception(wrapper)
        assert mode is FailureMode.TRANSIENT_TRANSPORT

    def test_chain_walks_cause_not_just_context(self):
        """`raise X from Y` sets __cause__; explicit chain must be walked."""
        inner = _make_remote_protocol_error()
        outer = RuntimeError("higher-level wrap")
        outer.__cause__ = inner
        mode = FailbackStateMachine.classify_exception(outer)
        assert mode is FailureMode.TRANSIENT_TRANSPORT

    def test_chain_walks_context_when_no_cause(self):
        """Implicit `except` chains expose __context__; classifier handles both."""
        inner = _make_closed_resource_error()
        outer = RuntimeError("implicit chain")
        outer.__context__ = inner
        # No explicit __cause__ — must fall through to __context__
        outer.__cause__ = None
        mode = FailbackStateMachine.classify_exception(outer)
        assert mode is FailureMode.TRANSIENT_TRANSPORT

    def test_chain_cycle_protection(self):
        """Self-referential chain must not cause infinite walk."""
        inner = _make_remote_protocol_error()
        outer = RuntimeError("cycle")
        outer.__cause__ = inner
        inner.__cause__ = outer  # cycle
        # Should not hang or recurse infinitely; should still find the
        # transient transport class somewhere in the chain.
        mode = FailbackStateMachine.classify_exception(outer)
        assert mode is FailureMode.TRANSIENT_TRANSPORT

    def test_chain_max_depth_respected(self):
        """Walk caps at max_depth to bound work on adversarial chains."""
        from backend.core.ouroboros.governance.candidate_generator import (
            _walk_exception_chain,
        )
        # Build a long linear chain of plain exceptions (no transport class)
        deepest = ValueError("leaf")
        current: BaseException = deepest
        for i in range(50):
            outer = RuntimeError(f"level-{i}")
            outer.__cause__ = current
            current = outer
        chain = _walk_exception_chain(current, max_depth=8)
        assert len(chain) == 8

    def test_classification_falls_back_to_existing_rules_on_unrelated(self):
        """Plain RuntimeError still routes via existing rules."""
        mode = FailbackStateMachine.classify_exception(asyncio.TimeoutError())
        assert mode is FailureMode.TIMEOUT

    def test_classification_recognizes_classic_connection_error_unchanged(self):
        """Existing ConnectionError handling is preserved."""
        mode = FailbackStateMachine.classify_exception(
            ConnectionRefusedError("conn refused")
        )
        assert mode is FailureMode.CONNECTION_ERROR

    def test_content_failure_outranks_transient_transport(self):
        """Content failures still beat infra classification — they say
        'don't penalize the provider', and that priority must be preserved.
        """
        class RemoteProtocolError(Exception):
            pass
        # Outermost message includes a content-failure marker
        outer = RemoteProtocolError("diff_apply_failed: stale")
        mode = FailbackStateMachine.classify_exception(outer)
        assert mode is FailureMode.CONTENT_FAILURE


class TestTransientTransportRecoveryParams:
    """The new mode's recovery profile is appropriately short."""

    def test_recovery_eta_starts_at_5s_base(self):
        fsm = FailbackStateMachine()
        fsm.record_primary_failure(mode=FailureMode.TRANSIENT_TRANSPORT)
        eta = fsm.recovery_eta()
        # First failure: base_s * 2^0 = 5s
        delay = eta - fsm._last_failure_at
        assert 4.9 <= delay <= 5.1

    def test_recovery_eta_caps_at_30s(self):
        fsm = FailbackStateMachine()
        for _ in range(10):
            fsm.record_primary_failure(mode=FailureMode.TRANSIENT_TRANSPORT)
        eta = fsm.recovery_eta()
        delay = eta - fsm._last_failure_at
        assert delay <= 30.1

    def test_transient_recovery_much_shorter_than_connection_error(self):
        """The whole point of the new mode: it recovers far faster.

        With 3 consecutive failures:
        - TRANSIENT_TRANSPORT: 5 * 4 = 20s (capped at 30)
        - CONNECTION_ERROR: 120 * 4 = 480s
        """
        fsm_transient = FailbackStateMachine()
        for _ in range(3):
            fsm_transient.record_primary_failure(mode=FailureMode.TRANSIENT_TRANSPORT)
        transient_delay = (
            fsm_transient.recovery_eta() - fsm_transient._last_failure_at
        )

        fsm_conn = FailbackStateMachine()
        for _ in range(3):
            fsm_conn.record_primary_failure(mode=FailureMode.CONNECTION_ERROR)
        conn_delay = fsm_conn.recovery_eta() - fsm_conn._last_failure_at

        assert transient_delay < conn_delay
        assert transient_delay <= 30.0
        assert conn_delay >= 480.0

    def test_should_attempt_primary_after_short_backoff(self):
        """After 5s base, should_attempt_primary returns True past ETA."""
        fsm = FailbackStateMachine()
        fsm.record_primary_failure(mode=FailureMode.TRANSIENT_TRANSPORT)
        # Force ETA into the past by mutating _last_failure_at
        fsm._last_failure_at = time.monotonic() - 10.0
        assert fsm.should_attempt_primary() is True


class TestPerOpTier0Rotation:
    """Per-op rotation guard for misclassification belt-and-suspenders."""

    def _make_gen_with_tier0(self, threshold: int = 2, window_s: float = 30.0):
        """Build a CandidateGenerator with a mock Tier 0 provider."""
        import os
        os.environ["OUROBOROS_TIER0_SKIP_THRESHOLD"] = str(threshold)
        os.environ["OUROBOROS_TIER0_SKIP_WINDOW_S"] = str(window_s)
        try:
            primary = _make_mock_provider(name="primary")
            fallback = _make_mock_provider(name="fallback")
            tier0 = _make_mock_provider(name="tier0")
            type(tier0).is_available = PropertyMock(return_value=True)
            type(tier0)._realtime_enabled = PropertyMock(return_value=True)
            gen = CandidateGenerator(primary=primary, fallback=fallback, tier0=tier0)
            return gen
        finally:
            os.environ.pop("OUROBOROS_TIER0_SKIP_THRESHOLD", None)
            os.environ.pop("OUROBOROS_TIER0_SKIP_WINDOW_S", None)

    def test_zero_failures_does_not_skip(self):
        gen = self._make_gen_with_tier0()
        assert gen._should_skip_tier0_for_op() is False

    def test_one_failure_below_threshold_does_not_skip(self):
        gen = self._make_gen_with_tier0(threshold=2)
        gen._record_tier0_failure()
        assert gen._should_skip_tier0_for_op() is False

    def test_threshold_failures_within_window_skips(self):
        gen = self._make_gen_with_tier0(threshold=2, window_s=30.0)
        gen._record_tier0_failure()
        gen._record_tier0_failure()
        assert gen._should_skip_tier0_for_op() is True

    def test_threshold_failures_outside_window_does_not_skip(self):
        gen = self._make_gen_with_tier0(threshold=2, window_s=30.0)
        gen._record_tier0_failure()
        gen._record_tier0_failure()
        # Force the most-recent failure timestamp into the past
        gen._last_tier0_failure_at = time.monotonic() - 60.0
        assert gen._should_skip_tier0_for_op() is False

    def test_success_resets_counter(self):
        gen = self._make_gen_with_tier0(threshold=2)
        gen._record_tier0_failure()
        gen._record_tier0_failure()
        assert gen._should_skip_tier0_for_op() is True
        gen._record_tier0_success()
        assert gen._consecutive_tier0_failures == 0
        assert gen._should_skip_tier0_for_op() is False

    def test_threshold_env_override_respected(self):
        gen = self._make_gen_with_tier0(threshold=5)
        for _ in range(4):
            gen._record_tier0_failure()
        assert gen._should_skip_tier0_for_op() is False
        gen._record_tier0_failure()
        assert gen._should_skip_tier0_for_op() is True

    def test_window_env_override_respected(self):
        gen = self._make_gen_with_tier0(threshold=2, window_s=5.0)
        gen._record_tier0_failure()
        gen._record_tier0_failure()
        assert gen._should_skip_tier0_for_op() is True
        # 6 seconds elapsed → past the 5s window
        gen._last_tier0_failure_at = time.monotonic() - 6.0
        assert gen._should_skip_tier0_for_op() is False

    def test_independence_from_fsm_mode(self):
        """Rotation guard fires regardless of FSM classifier output.

        The whole point: even if classify_exception mis-routes a transport
        flap to TIMEOUT (default), the rotation guard still kicks in.
        """
        gen = self._make_gen_with_tier0(threshold=2)
        # Don't touch FSM at all — rotation must work standalone
        assert gen.fsm._failure_mode is None
        gen._record_tier0_failure()
        gen._record_tier0_failure()
        assert gen._should_skip_tier0_for_op() is True


class TestFsmDeepBackoffGeneralization:
    """Generalized backoff guard honors any failure mode, not just CONNECTION_ERROR."""

    def test_transient_transport_blocks_should_attempt_when_in_backoff(self):
        """TRANSIENT_TRANSPORT in backoff window prevents retry."""
        fsm = FailbackStateMachine()
        fsm.record_primary_failure(mode=FailureMode.TRANSIENT_TRANSPORT)
        # Within the 5s base window
        assert fsm.should_attempt_primary() is False

    def test_transient_transport_allows_after_window(self):
        fsm = FailbackStateMachine()
        fsm.record_primary_failure(mode=FailureMode.TRANSIENT_TRANSPORT)
        fsm._last_failure_at = time.monotonic() - 6.0
        assert fsm.should_attempt_primary() is True

    def test_timeout_mode_recovery_eta_unchanged(self):
        """Existing TIMEOUT recovery profile preserved."""
        fsm = FailbackStateMachine()
        fsm.record_primary_failure(mode=FailureMode.TIMEOUT)
        delay = fsm.recovery_eta() - fsm._last_failure_at
        assert 44.9 <= delay <= 45.1


class TestProductionFailureScenario:
    """Reproduce the exact pattern from bt-2026-04-12-005521.

    9 consecutive ops dying because:
      1. Anthropic SDK raises APIConnectionError(cause=RemoteProtocolError(...))
      2. Old classifier returned TIMEOUT (45s/300s recovery)
      3. CONNECTION_ERROR-only deep-backoff guard never fired
      4. Each op hit the same dead transport on the next attempt
      5. Fallback also failed → all_providers_exhausted

    The fix should: classify as TRANSIENT_TRANSPORT (5s/30s), trigger the
    generalized deep-backoff guard, and after 2 consecutive failures the
    per-op rotation kicks in to skip Tier 0 entirely until recovery.
    """

    def test_repro_full_chain_ends_in_short_backoff(self):
        # Step 1: classifier sees the wrapper
        inner = _make_remote_protocol_error()
        wrapper = _make_api_connection_error_wrapping(inner)
        mode = FailbackStateMachine.classify_exception(wrapper)
        assert mode is FailureMode.TRANSIENT_TRANSPORT, (
            f"misclassified — got {mode}, the bug would still be present"
        )

        # Step 2: FSM records the failure with the correct mode
        fsm = FailbackStateMachine()
        fsm.record_primary_failure(mode=mode)
        eta = fsm.recovery_eta()
        delay = eta - fsm._last_failure_at
        assert delay <= 30.0, (
            f"recovery delay {delay}s exceeds the 30s cap — bug still present"
        )

        # Step 3: should_attempt_primary returns False inside the window
        assert fsm.should_attempt_primary() is False

        # Step 4: After ~5s the FSM unblocks (TRANSIENT_TRANSPORT base)
        fsm._last_failure_at = time.monotonic() - 5.5
        assert fsm.should_attempt_primary() is True

    def test_repro_rotation_engages_after_two_failures(self):
        """Per-op rotation engages even if the FSM mis-routed."""
        primary = _make_mock_provider(name="primary")
        fallback = _make_mock_provider(name="fallback")
        tier0 = _make_mock_provider(name="tier0")
        type(tier0).is_available = PropertyMock(return_value=True)
        type(tier0)._realtime_enabled = PropertyMock(return_value=True)

        gen = CandidateGenerator(primary=primary, fallback=fallback, tier0=tier0)
        # Default: threshold=2, window=30s
        assert gen._tier0_skip_threshold == 2
        assert gen._tier0_skip_window_s == 30.0

        # Op 1 fails on Tier 0
        gen._record_tier0_failure()
        assert gen._should_skip_tier0_for_op() is False  # below threshold

        # Op 2 fails on Tier 0
        gen._record_tier0_failure()
        # Now skip — Op 3 will route directly to Claude fallback
        assert gen._should_skip_tier0_for_op() is True

    def test_repro_rotation_clears_on_recovery(self):
        """Once Tier 0 recovers, the rotation counter clears so subsequent
        ops resume the cheap path.
        """
        primary = _make_mock_provider(name="primary")
        fallback = _make_mock_provider(name="fallback")
        tier0 = _make_mock_provider(name="tier0")
        type(tier0).is_available = PropertyMock(return_value=True)

        gen = CandidateGenerator(primary=primary, fallback=fallback, tier0=tier0)
        gen._record_tier0_failure()
        gen._record_tier0_failure()
        assert gen._should_skip_tier0_for_op() is True

        gen._record_tier0_success()
        assert gen._should_skip_tier0_for_op() is False
        assert gen._consecutive_tier0_failures == 0


class TestExceptionChainHelper:
    """Direct unit tests on _walk_exception_chain helper."""

    def test_returns_single_element_for_unchained(self):
        from backend.core.ouroboros.governance.candidate_generator import (
            _walk_exception_chain,
        )
        chain = _walk_exception_chain(ValueError("alone"))
        assert len(chain) == 1
        assert isinstance(chain[0], ValueError)

    def test_returns_outermost_first(self):
        from backend.core.ouroboros.governance.candidate_generator import (
            _walk_exception_chain,
        )
        inner = ValueError("inner")
        outer = RuntimeError("outer")
        outer.__cause__ = inner
        chain = _walk_exception_chain(outer)
        assert len(chain) == 2
        assert isinstance(chain[0], RuntimeError)
        assert isinstance(chain[1], ValueError)

    def test_handles_none_cause(self):
        from backend.core.ouroboros.governance.candidate_generator import (
            _walk_exception_chain,
        )
        exc = RuntimeError("nothing")
        exc.__cause__ = None
        exc.__context__ = None
        chain = _walk_exception_chain(exc)
        assert chain == (exc,)


# ---------------------------------------------------------------------------
# Context Overflow classification
# ---------------------------------------------------------------------------


class TestContextOverflowClassification:
    """Verify tool_loop_budget_exceeded / tool_loop_context_overflow map to
    CONTEXT_OVERFLOW, not TIMEOUT."""

    def test_budget_exceeded_classified_as_context_overflow(self):
        exc = RuntimeError("tool_loop_budget_exceeded:142000")
        mode = FailbackStateMachine.classify_exception(exc)
        assert mode is FailureMode.CONTEXT_OVERFLOW

    def test_context_overflow_classified_as_context_overflow(self):
        exc = RuntimeError("tool_loop_context_overflow:155000")
        mode = FailbackStateMachine.classify_exception(exc)
        assert mode is FailureMode.CONTEXT_OVERFLOW

    def test_context_overflow_zero_backoff(self):
        from backend.core.ouroboros.governance.candidate_generator import (
            _RECOVERY_PARAMS,
        )
        params = _RECOVERY_PARAMS[FailureMode.CONTEXT_OVERFLOW]
        assert params["base_s"] == 0.0
        assert params["max_s"] == 0.0

    def test_context_overflow_does_not_penalize_fsm(self):
        fsm = FailbackStateMachine()
        fsm.record_primary_failure(mode=FailureMode.CONTEXT_OVERFLOW)
        eta = fsm.recovery_eta()
        assert eta <= time.monotonic()

    def test_context_overflow_not_misclassified_as_timeout(self):
        exc = RuntimeError("tool_loop_budget_exceeded:131500")
        mode = FailbackStateMachine.classify_exception(exc)
        assert mode is not FailureMode.TIMEOUT


# ---------------------------------------------------------------------------
# TestFallbackSemStarvation — post-acquire deadline refresh
# ---------------------------------------------------------------------------


class TestFallbackSemStarvation:
    """Verify _call_fallback computes budget AFTER acquiring _fallback_sem.

    When multiple ops queue behind the semaphore, the original parent
    deadline burns while waiting.  The fix: budget computation + deadline
    refresh happen post-acquire, so the fallback always gets at least
    _FALLBACK_MIN_GUARANTEED_S regardless of queue wait time.
    """

    @pytest.fixture()
    def ctx(self) -> OperationContext:
        return _make_context()

    @pytest.mark.asyncio
    async def test_sem_wait_does_not_starve_fallback(
        self, ctx: OperationContext
    ) -> None:
        """Simulate sem contention burning the parent deadline.

        Slot 1 holds the semaphore for 3s.  Slot 2 queues and its parent
        deadline (4s) expires during the wait.  Post-acquire refresh should
        still give it a viable window.
        """
        call_count = 0
        received_deadlines: list = []

        async def _slow_then_fast(ctx_arg, deadline_arg):
            nonlocal call_count
            call_count += 1
            received_deadlines.append(deadline_arg)
            if call_count == 1:
                await asyncio.sleep(3.0)
            return _make_generation_result(provider_name="fallback")

        primary = _make_mock_provider(
            name="primary", generate_side_effect=RuntimeError("down")
        )
        fallback = _make_mock_provider(name="fallback")
        fallback.generate.side_effect = _slow_then_fast

        gen = CandidateGenerator(
            primary=primary, fallback=fallback, fallback_concurrency=1,
        )

        short_deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=4)

        results = await asyncio.gather(
            gen._call_fallback(ctx, short_deadline),
            gen._call_fallback(ctx, short_deadline),
        )
        assert len(results) == 2
        assert all(r.provider_name == "fallback" for r in results)

        # The second call's deadline should have been refreshed — it queued
        # for ~3s, parent had 4s, so post-acquire parent_remaining ≈ 1s.
        # Refresh should bump it to _FALLBACK_MIN_GUARANTEED_S (90s).
        second_remaining = (
            received_deadlines[1] - datetime.now(tz=timezone.utc)
        ).total_seconds()
        assert second_remaining > 60.0, (
            f"Queued fallback was starved: only {second_remaining:.1f}s"
        )

    @pytest.mark.asyncio
    async def test_expired_parent_still_gets_guaranteed_window(
        self, ctx: OperationContext
    ) -> None:
        """Even with parent_remaining == 0, fallback gets a guaranteed window."""
        received_deadline: list = []

        async def _capture(ctx_arg, deadline_arg):
            received_deadline.append(deadline_arg)
            return _make_generation_result(provider_name="fallback")

        primary = _make_mock_provider(
            name="primary", generate_side_effect=RuntimeError("down")
        )
        fallback = _make_mock_provider(name="fallback")
        fallback.generate.side_effect = _capture

        gen = CandidateGenerator(primary=primary, fallback=fallback)

        # Deadline already expired
        expired = datetime.now(tz=timezone.utc) - timedelta(seconds=5)
        result = await gen._call_fallback(ctx, expired)
        assert result.provider_name == "fallback"
        assert len(received_deadline) == 1

        refreshed = (
            received_deadline[0] - datetime.now(tz=timezone.utc)
        ).total_seconds()
        assert refreshed > 60.0, (
            f"Expired parent should refresh: got {refreshed:.1f}s"
        )

    @pytest.mark.asyncio
    async def test_plan_fallback_sem_post_acquire_budget(self) -> None:
        """plan() fallback path also computes budget post-acquire."""
        primary = _make_mock_provider(name="primary")
        primary.plan = AsyncMock(side_effect=RuntimeError("plan down"))

        received_deadline: list = []

        async def _plan_capture(prompt, deadline_arg):
            received_deadline.append(deadline_arg)
            return "plan text"

        fallback = _make_mock_provider(name="fallback")
        fallback.plan = AsyncMock(side_effect=_plan_capture)

        gen = CandidateGenerator(primary=primary, fallback=fallback)
        gen.fsm.record_primary_failure()  # Force FALLBACK_ACTIVE

        depleted = datetime.now(tz=timezone.utc) + timedelta(seconds=3)
        result = await gen.plan("Generate a plan", depleted)
        assert result == "plan text"
        assert len(received_deadline) == 1

        refreshed = (
            received_deadline[0] - datetime.now(tz=timezone.utc)
        ).total_seconds()
        assert refreshed > 60.0, (
            f"Plan fallback should refresh depleted deadline: {refreshed:.1f}s"
        )


# ---------------------------------------------------------------------------
# TestExhaustionInstrumentation
# ---------------------------------------------------------------------------


class TestExhaustionInstrumentation:
    """Verify the ``_raise_exhausted`` helper contract.

    Every ``all_providers_exhausted`` raise must emit a structured
    breadcrumb log line AND attach a ``.exhaustion_report`` dict to the
    raised ``RuntimeError`` so downstream battle-test audits can find the
    root cause without re-parsing free-form log messages.
    """

    def _required_report_keys(self) -> set:
        return {
            "event_n",
            "cause",
            "fsm_state",
            "fsm_failure_mode",
            "fsm_consecutive_failures",
            "tier0_consecutive_failures",
            "primary_name",
            "fallback_name",
            "tier0_name",
        }

    @pytest.mark.asyncio
    async def test_queue_only_dispatch_attaches_report(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """QUEUE_ONLY on the standard cascade → cause=queue_only_dispatch."""
        primary = _make_mock_provider(name="primary")
        fallback = _make_mock_provider(name="fallback")
        gen = CandidateGenerator(primary=primary, fallback=fallback)
        gen.fsm.record_primary_failure()
        gen.fsm.record_fallback_failure(mode=FailureMode.CONNECTION_ERROR)
        assert gen.fsm.state is FailbackState.QUEUE_ONLY

        ctx = _make_context(op_id="op-queue-001")
        deadline = _make_deadline(5.0)

        caplog.set_level("ERROR")
        with pytest.raises(RuntimeError, match="all_providers_exhausted") as ei:
            await gen.generate(ctx, deadline)

        err = ei.value
        report = getattr(err, "exhaustion_report", None)
        assert isinstance(report, dict)
        assert self._required_report_keys().issubset(report.keys())
        assert report["cause"] == "queue_only_dispatch"
        assert report["event_n"] == 1
        assert report["fsm_state"] == "QUEUE_ONLY"
        assert report["primary_name"] == "primary"
        assert report["fallback_name"] == "fallback"
        assert report["op_id"] == "op-queue-001"
        assert "remaining_s" in report

        assert any(
            "EXHAUSTION" in rec.message
            and "cause=queue_only_dispatch" in rec.message
            for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_fallback_failed_chains_cause_and_err_class(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Both-providers-down → cause=fallback_failed with chained exc info."""
        primary = _make_mock_provider(
            name="primary",
            generate_side_effect=RuntimeError("primary down"),
        )
        fallback = _make_mock_provider(
            name="fallback",
            generate_side_effect=TimeoutError("fallback tcp timeout"),
        )
        gen = CandidateGenerator(primary=primary, fallback=fallback)

        ctx = _make_context(op_id="op-fallback-002")
        deadline = _make_deadline(5.0)

        caplog.set_level("ERROR")
        with pytest.raises(RuntimeError, match="all_providers_exhausted") as ei:
            await gen.generate(ctx, deadline)

        err = ei.value
        report = getattr(err, "exhaustion_report")
        assert report["cause"] == "fallback_failed"
        assert report["fallback_err_class"] == "TimeoutError"
        assert "fallback tcp timeout" in report["fallback_err_msg"]
        assert "fallback_failure_mode" in report
        assert err.__cause__ is not None, "should chain from fallback exc"

    @pytest.mark.asyncio
    async def test_budget_starved_carries_sem_and_budget_breadcrumbs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Depleted parent budget → cause=fallback_budget_starved with
        sem_wait_s, parent_remaining_s, and fallback_budget_s.

        To force the ``remaining < _MIN_VIABLE_FALLBACK_S`` branch we
        monkey-patch the module constants — guarantees the test triggers
        the budget-starved raise instead of the sem-wait-refresh path.
        """
        from backend.core.ouroboros.governance import candidate_generator as cg

        primary = _make_mock_provider(name="primary")
        fallback = _make_mock_provider(name="fallback")
        gen = CandidateGenerator(primary=primary, fallback=fallback)
        gen.fsm.record_primary_failure()  # FALLBACK_ACTIVE

        ctx = _make_context(op_id="op-starved-003")
        deadline = _make_deadline(2.0)

        caplog.set_level("ERROR")
        with (
            patch.object(cg, "_FALLBACK_MIN_GUARANTEED_S", 0.5),
            patch.object(cg, "_MIN_VIABLE_FALLBACK_S", 100.0),
            patch.object(CandidateGenerator, "_FALLBACK_MAX_TIMEOUT_S", 0.5),
        ):
            with pytest.raises(RuntimeError, match="all_providers_exhausted") as ei:
                await gen._call_fallback(ctx, deadline)

        report = getattr(ei.value, "exhaustion_report")
        assert report["cause"] == "fallback_budget_starved"
        assert "sem_wait_s" in report
        assert "parent_remaining_s" in report
        assert "fallback_budget_s" in report
        assert report["min_viable_fallback_s"] == 100.0
        fallback.generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_event_counter_increments_across_raises(self) -> None:
        """Multiple exhaustion raises on the same generator must share
        a monotonic event_n counter so the audit can sequence events."""
        primary = _make_mock_provider(name="primary")
        fallback = _make_mock_provider(name="fallback")
        gen = CandidateGenerator(primary=primary, fallback=fallback)
        gen.fsm.record_primary_failure()
        gen.fsm.record_fallback_failure(mode=FailureMode.CONNECTION_ERROR)
        assert gen.fsm.state is FailbackState.QUEUE_ONLY

        ctx = _make_context(op_id="op-counter-004")
        deadline = _make_deadline(3.0)

        with pytest.raises(RuntimeError) as e1:
            await gen.generate(ctx, deadline)
        with pytest.raises(RuntimeError) as e2:
            await gen.generate(ctx, deadline)
        with pytest.raises(RuntimeError) as e3:
            await gen.generate(ctx, deadline)

        assert getattr(e1.value, "exhaustion_report")["event_n"] == 1
        assert getattr(e2.value, "exhaustion_report")["event_n"] == 2
        assert getattr(e3.value, "exhaustion_report")["event_n"] == 3
        assert gen._exhaustion_events == 3

    def test_raise_exhausted_message_keeps_substring_contract(self) -> None:
        """``str(exc)`` must still contain ``all_providers_exhausted`` so
        every downstream substring check (orchestrator _INFRA_PATTERNS,
        ProviderExhaustionWatcher, pytest ``match=`` regexes) keeps
        working after the cause suffix was added."""
        primary = _make_mock_provider(name="primary")
        fallback = _make_mock_provider(name="fallback")
        gen = CandidateGenerator(primary=primary, fallback=fallback)
        with pytest.raises(RuntimeError) as ei:
            gen._raise_exhausted("queue_only_dispatch")
        assert "all_providers_exhausted" in str(ei.value)
        assert ":queue_only_dispatch" in str(ei.value)

    @pytest.mark.asyncio
    async def test_fallback_round_starved_cause_tag_on_tool_loop_gate(
        self,
    ) -> None:
        """``ToolLoopCoordinator``'s pre-round viability gate raises with a
        ``tool_loop_round_budget_starved`` marker. ``_call_fallback`` must
        detect that marker in ``str(exc)`` and promote the exhaustion
        breadcrumb cause from the generic ``fallback_failed`` to the more
        specific ``fallback_round_starved``, so battle-test grep audits
        can distinguish structural round starvation from transport
        failures without reading full exception traces.
        """
        primary = _make_mock_provider(
            name="primary",
            generate_side_effect=RuntimeError("primary down"),
        )
        fallback = _make_mock_provider(
            name="fallback",
            generate_side_effect=RuntimeError(
                "tool_loop_round_budget_starved:round=1,"
                "remaining=0.37s,min_per_round=3.00s"
            ),
        )
        gen = CandidateGenerator(primary=primary, fallback=fallback)

        ctx = _make_context(op_id="op-round-starved-005")
        deadline = _make_deadline(5.0)

        with pytest.raises(RuntimeError, match="all_providers_exhausted") as ei:
            await gen.generate(ctx, deadline)

        report = getattr(ei.value, "exhaustion_report")
        assert report["cause"] == "fallback_round_starved", (
            f"expected fallback_round_starved, got {report['cause']!r}"
        )
        # The generic transport fields still populate (we did not skip the
        # exception handler) — only the cause tag changes.
        assert "fallback_err_class" in report
        assert "tool_loop_round_budget_starved" in report["fallback_err_msg"]

    @pytest.mark.asyncio
    async def test_fallback_failed_cause_tag_on_plain_transport_error(
        self,
    ) -> None:
        """Counterpart to the round-starved test: verify that a non-gate
        transport failure (e.g. ``TimeoutError``) still tags as the plain
        ``fallback_failed`` cause, proving the tag promotion is scoped to
        the marker substring and not accidentally triggered by unrelated
        errors.
        """
        primary = _make_mock_provider(
            name="primary",
            generate_side_effect=RuntimeError("primary down"),
        )
        fallback = _make_mock_provider(
            name="fallback",
            generate_side_effect=TimeoutError("tcp read timeout"),
        )
        gen = CandidateGenerator(primary=primary, fallback=fallback)

        ctx = _make_context(op_id="op-plain-fail-006")
        deadline = _make_deadline(5.0)

        with pytest.raises(RuntimeError, match="all_providers_exhausted") as ei:
            await gen.generate(ctx, deadline)

        report = getattr(ei.value, "exhaustion_report")
        assert report["cause"] == "fallback_failed"
        assert report["fallback_err_class"] == "TimeoutError"
