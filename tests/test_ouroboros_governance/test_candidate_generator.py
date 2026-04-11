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
    async def test_expired_deadline_raises_immediately(
        self, ctx: OperationContext
    ) -> None:
        """An already-past deadline should raise, not hang."""
        primary = _make_mock_provider(name="primary")
        fallback = _make_mock_provider(name="fallback")
        gen = CandidateGenerator(primary=primary, fallback=fallback)
        past_deadline = datetime.now(tz=timezone.utc) - timedelta(seconds=1)

        with pytest.raises((asyncio.TimeoutError, RuntimeError)):
            await gen.generate(ctx, past_deadline)

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
