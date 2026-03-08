"""End-to-end integration tests for the Intent Engine pipeline.

Exercises the full signal flow across existing modules:
TestWatcher -> IntentEngine -> GovernedLoopService (mocked),
ErrorInterceptor -> IntentEngine -> narrate_fn,
and deduplication blocking repeated signals.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.intent.engine import (
    IntentEngine,
    IntentEngineConfig,
)
from backend.core.ouroboros.governance.intent.error_interceptor import (
    ErrorInterceptor,
)
from backend.core.ouroboros.governance.intent.signals import IntentSignal
from backend.core.ouroboros.governance.intent.test_watcher import TestFailure


# ---------------------------------------------------------------------------
# Test 1: Stable failure submits to governed pipeline
# ---------------------------------------------------------------------------


class TestE2EStableFailureSubmitsToGovernedPipeline:
    """Full flow: TestWatcher detects stable failure -> IntentEngine submits to GLS."""

    @pytest.mark.asyncio
    async def test_e2e_stable_failure_submits_to_governed_pipeline(self) -> None:
        # -- Arrange --
        mock_gls = AsyncMock()
        mock_gls.submit = AsyncMock(return_value=MagicMock(op_id="op-e2e-001"))

        config = IntentEngineConfig(
            repos={"jarvis": "."},
            test_dirs={"jarvis": "tests/"},
            dedup_cooldown_s=0.0,
            file_cooldown_s=0.0,
            signal_cooldown_s=0.0,
        )

        engine = IntentEngine(
            config=config,
            governed_loop_service=mock_gls,
        )
        await engine.start()

        try:
            # Get the watcher that was created for "jarvis"
            watcher = engine._watchers["jarvis"]

            # Create a test failure
            failure = TestFailure(
                test_id="tests/test_utils.py::test_edge_case",
                file_path="tests/test_utils.py",
                error_text="AssertionError: expected 3, got 2",
            )

            # -- Act --
            # First call: streak=1 -> not stable yet, no signals
            signals_run1 = watcher.process_failures([failure])
            assert len(signals_run1) == 0, (
                "First failure should not produce a signal (not stable yet)"
            )

            # Second call: streak=2 -> stable, should emit 1 signal
            signals_run2 = watcher.process_failures([failure])
            assert len(signals_run2) == 1, (
                "Second consecutive failure should produce a stable signal"
            )
            signal = signals_run2[0]
            assert signal.stable is True

            # Submit the stable signal through the engine
            result = await engine.handle_signal(signal)

            # -- Assert --
            assert result == "submitted"
            mock_gls.submit.assert_called_once()

            # Verify the OperationContext passed to GLS
            call_args = mock_gls.submit.call_args
            ctx = call_args[0][0]
            assert "tests/test_utils.py" in ctx.target_files
            assert call_args[1]["trigger_source"] == "intent:test_failure"
        finally:
            engine.stop()


# ---------------------------------------------------------------------------
# Test 2: Observe-only stack trace
# ---------------------------------------------------------------------------


class TestE2EObserveOnlyStackTrace:
    """ErrorInterceptor emits a signal -> IntentEngine observes (no submit) + narrates."""

    @pytest.mark.asyncio
    async def test_e2e_observe_only_stack_trace(self) -> None:
        # -- Arrange --
        mock_gls = AsyncMock()
        mock_gls.submit = AsyncMock()

        narration_log: list[str] = []

        async def mock_narrate(message: str) -> None:
            narration_log.append(message)

        config = IntentEngineConfig(
            repos={"jarvis": "."},
            test_dirs={"jarvis": "tests/"},
            dedup_cooldown_s=0.0,
            file_cooldown_s=0.0,
            signal_cooldown_s=0.0,
        )

        engine = IntentEngine(
            config=config,
            governed_loop_service=mock_gls,
            narrate_fn=mock_narrate,
        )

        # Create an ErrorInterceptor and collect signals
        interceptor = ErrorInterceptor(repo="jarvis")
        collected_signals: list[IntentSignal] = []
        interceptor.on_signal = collected_signals.append

        # Use a unique logger name to avoid cross-test interference
        test_logger = logging.getLogger("test.e2e.observe_only_stack_trace")
        test_logger.setLevel(logging.DEBUG)
        interceptor.install(test_logger)

        try:
            # -- Act --
            # Log an error message to trigger the interceptor
            test_logger.error("Database connection pool exhausted")

            # The interceptor should have collected one signal
            assert len(collected_signals) == 1
            signal = collected_signals[0]

            # Route the signal through the engine
            result = await engine.handle_signal(signal)

            # -- Assert --
            assert result == "observed"

            # Narration should have been called
            assert len(narration_log) == 1
            narration_text = narration_log[0].lower()
            assert "seeing" in narration_text or "error" in narration_text or "stack_trace" in narration_text

            # GLS.submit should NOT have been called
            mock_gls.submit.assert_not_called()
        finally:
            interceptor.uninstall(test_logger)


# ---------------------------------------------------------------------------
# Test 3: Dedup blocks repeated signals
# ---------------------------------------------------------------------------


class TestE2EDedupBlocksRepeatedSignals:
    """Same signal submitted twice -> first succeeds, second is deduplicated."""

    @pytest.mark.asyncio
    async def test_e2e_dedup_blocks_repeated_signals(self) -> None:
        # -- Arrange --
        mock_gls = AsyncMock()
        mock_gls.submit = AsyncMock(return_value=None)

        config = IntentEngineConfig(
            repos={"jarvis": "."},
            test_dirs={"jarvis": "tests/"},
            dedup_cooldown_s=300.0,  # Long cooldown to ensure dedup triggers
            file_cooldown_s=0.0,
            signal_cooldown_s=0.0,
        )

        engine = IntentEngine(
            config=config,
            governed_loop_service=mock_gls,
        )

        # Create a stable test-failure signal
        signal = IntentSignal(
            source="intent:test_failure",
            target_files=("tests/test_utils.py",),
            repo="jarvis",
            description="Stable test failure: test_edge_case (streak=2)",
            evidence={"signature": "AssertionError:test_utils:42"},
            confidence=0.9,
            stable=True,
        )

        # -- Act --
        result1 = await engine.handle_signal(signal)
        result2 = await engine.handle_signal(signal)

        # -- Assert --
        assert result1 == "submitted"
        assert result2 == "deduplicated"
        mock_gls.submit.assert_called_once()
