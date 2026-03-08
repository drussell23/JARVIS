"""Tests for IntentEngine orchestrator.

Validates the central routing logic that ties together DedupTracker,
RateLimiter, TestWatcher, and GovernedLoopService submission.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from backend.core.ouroboros.governance.intent.engine import (
    IntentEngine,
    IntentEngineConfig,
    _build_operation_context,
)
from backend.core.ouroboros.governance.intent.signals import IntentSignal


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> IntentEngineConfig:
    """Minimal config with tight limits for fast tests."""
    return IntentEngineConfig(
        repos={"jarvis": "/tmp/fake-repo"},
        test_dirs={"jarvis": "tests/"},
        poll_interval_s=1.0,
        dedup_cooldown_s=300.0,
        max_ops_per_hour=5,
        max_ops_per_day=20,
        file_cooldown_s=600.0,
        signal_cooldown_s=300.0,
    )


@pytest.fixture
def mock_gls() -> AsyncMock:
    """Mock GovernedLoopService with an async submit method."""
    gls = AsyncMock()
    gls.submit = AsyncMock(return_value=None)
    return gls


def _make_signal(
    *,
    source: str = "intent:test_failure",
    target_files: tuple[str, ...] = ("backend/core/foo.py",),
    repo: str = "jarvis",
    description: str = "Stable test failure: test_foo (streak=2): AssertionError",
    evidence: dict | None = None,
    confidence: float = 0.9,
    stable: bool = True,
) -> IntentSignal:
    """Factory for concise signal construction in tests."""
    if evidence is None:
        evidence = {"signature": "AssertionError:foo:42"}
    return IntentSignal(
        source=source,
        target_files=target_files,
        repo=repo,
        description=description,
        evidence=evidence,
        confidence=confidence,
        stable=stable,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEngineLifecycle:
    """test_engine_lifecycle — create, start, stop state transitions."""

    @pytest.mark.asyncio
    async def test_initial_state_inactive(self, config: IntentEngineConfig, mock_gls: AsyncMock) -> None:
        engine = IntentEngine(config=config, governed_loop_service=mock_gls)
        assert engine.state == "inactive"

    @pytest.mark.asyncio
    async def test_start_transitions_to_watching(self, config: IntentEngineConfig, mock_gls: AsyncMock) -> None:
        engine = IntentEngine(config=config, governed_loop_service=mock_gls)
        await engine.start()
        assert engine.state == "watching"

    @pytest.mark.asyncio
    async def test_stop_transitions_to_inactive(self, config: IntentEngineConfig, mock_gls: AsyncMock) -> None:
        engine = IntentEngine(config=config, governed_loop_service=mock_gls)
        await engine.start()
        assert engine.state == "watching"
        engine.stop()
        assert engine.state == "inactive"

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self, config: IntentEngineConfig, mock_gls: AsyncMock) -> None:
        engine = IntentEngine(config=config, governed_loop_service=mock_gls)
        await engine.start()
        assert engine.state == "watching"
        # Second start should be a no-op
        await engine.start()
        assert engine.state == "watching"


class TestEngineRoutesStableTestFailureToSubmit:
    """test_engine_routes_stable_test_failure_to_submit"""

    @pytest.mark.asyncio
    async def test_stable_test_failure_submitted(self, config: IntentEngineConfig, mock_gls: AsyncMock) -> None:
        engine = IntentEngine(config=config, governed_loop_service=mock_gls)
        await engine.start()

        signal = _make_signal(
            source="intent:test_failure",
            stable=True,
        )
        result = await engine.handle_signal(signal)

        assert result == "submitted"
        mock_gls.submit.assert_called_once()


class TestEngineRoutesObserveOnlyToNarrate:
    """test_engine_routes_observe_only_to_narrate"""

    @pytest.mark.asyncio
    async def test_stack_trace_observed_not_submitted(self, config: IntentEngineConfig, mock_gls: AsyncMock) -> None:
        engine = IntentEngine(config=config, governed_loop_service=mock_gls)
        await engine.start()

        signal = _make_signal(
            source="intent:stack_trace",
            stable=True,
        )
        result = await engine.handle_signal(signal)

        assert result == "observed"
        mock_gls.submit.assert_not_called()

    @pytest.mark.asyncio
    async def test_unstable_test_failure_observed(self, config: IntentEngineConfig, mock_gls: AsyncMock) -> None:
        engine = IntentEngine(config=config, governed_loop_service=mock_gls)
        await engine.start()

        signal = _make_signal(
            source="intent:test_failure",
            stable=False,
        )
        result = await engine.handle_signal(signal)

        assert result == "observed"
        mock_gls.submit.assert_not_called()

    @pytest.mark.asyncio
    async def test_narrate_fn_called_on_observe(self, config: IntentEngineConfig, mock_gls: AsyncMock) -> None:
        narrate = AsyncMock()
        engine = IntentEngine(
            config=config,
            governed_loop_service=mock_gls,
            narrate_fn=narrate,
        )
        await engine.start()

        signal = _make_signal(source="intent:stack_trace", stable=True)
        await engine.handle_signal(signal)

        narrate.assert_called_once()
        call_args = narrate.call_args[0][0]
        assert "intent:stack_trace" in call_args
        assert signal.repo in call_args


class TestEngineRejectsDuplicate:
    """test_engine_rejects_duplicate — same signal twice."""

    @pytest.mark.asyncio
    async def test_second_identical_signal_deduplicated(self, config: IntentEngineConfig, mock_gls: AsyncMock) -> None:
        engine = IntentEngine(config=config, governed_loop_service=mock_gls)
        await engine.start()

        signal = _make_signal(source="intent:test_failure", stable=True)

        first_result = await engine.handle_signal(signal)
        assert first_result == "submitted"

        # Same signal (same dedup_key) should be deduplicated
        second_result = await engine.handle_signal(signal)
        assert second_result == "deduplicated"

        # GLS.submit should only have been called once
        mock_gls.submit.assert_called_once()


class TestEngineRejectsRateLimited:
    """test_engine_rejects_rate_limited — exhaust hourly cap."""

    @pytest.mark.asyncio
    async def test_exceeds_hourly_cap(self, mock_gls: AsyncMock) -> None:
        # Config with max_ops_per_hour=1 and zero dedup cooldown
        config = IntentEngineConfig(
            repos={"jarvis": "/tmp/fake-repo"},
            test_dirs={"jarvis": "tests/"},
            poll_interval_s=1.0,
            dedup_cooldown_s=0.0,  # No dedup cooldown so second signal passes dedup
            max_ops_per_hour=1,
            max_ops_per_day=20,
            file_cooldown_s=0.0,
            signal_cooldown_s=0.0,
        )
        engine = IntentEngine(config=config, governed_loop_service=mock_gls)
        await engine.start()

        # First signal — should succeed
        signal_1 = _make_signal(
            source="intent:test_failure",
            stable=True,
            target_files=("file_a.py",),
            evidence={"signature": "err:a:1"},
        )
        result_1 = await engine.handle_signal(signal_1)
        assert result_1 == "submitted"

        # Second signal (different files/evidence to avoid dedup) — should be rate limited
        signal_2 = _make_signal(
            source="intent:test_failure",
            stable=True,
            target_files=("file_b.py",),
            evidence={"signature": "err:b:2"},
        )
        result_2 = await engine.handle_signal(signal_2)
        assert result_2 == "rate_limited"


class TestEngineBuildsOperationContextCorrectly:
    """test_engine_builds_operation_context_correctly"""

    @pytest.mark.asyncio
    async def test_submit_receives_correct_context(self, config: IntentEngineConfig, mock_gls: AsyncMock) -> None:
        engine = IntentEngine(config=config, governed_loop_service=mock_gls)
        await engine.start()

        target = ("backend/core/bar.py",)
        signal = _make_signal(
            source="intent:test_failure",
            stable=True,
            target_files=target,
            description="Stable failure in bar",
        )
        await engine.handle_signal(signal)

        mock_gls.submit.assert_called_once()
        call_args = mock_gls.submit.call_args

        # Positional arg 0 is the OperationContext
        ctx = call_args[0][0]
        assert ctx.target_files == target
        assert ctx.description == "Stable failure in bar"
        assert ctx.op_id.startswith("op-")

        # trigger_source kwarg
        assert call_args[1]["trigger_source"] == "intent:test_failure"

    @pytest.mark.asyncio
    async def test_build_operation_context_standalone(self) -> None:
        signal = _make_signal(
            target_files=("src/engine.py", "src/util.py"),
            description="Two-file failure",
            repo="prime",
        )
        ctx = _build_operation_context(signal)

        assert ctx.target_files == ("src/engine.py", "src/util.py")
        assert ctx.description == "Two-file failure"
        assert "prime" in ctx.op_id
        # Should start in CLASSIFY phase
        assert ctx.phase.name == "CLASSIFY"

    @pytest.mark.asyncio
    async def test_state_restored_after_submit(self, config: IntentEngineConfig, mock_gls: AsyncMock) -> None:
        """Engine state should be restored to previous state after submission."""
        engine = IntentEngine(config=config, governed_loop_service=mock_gls)
        await engine.start()
        assert engine.state == "watching"

        signal = _make_signal(source="intent:test_failure", stable=True)
        await engine.handle_signal(signal)

        # State should be restored to "watching" after submission
        assert engine.state == "watching"
