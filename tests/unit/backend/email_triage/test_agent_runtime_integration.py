"""Tests for agent runtime ↔ email triage wiring.

Verifies that _maybe_run_email_triage() captures the TriageCycleReport,
logs the summary, and uses the DLM single-leader guard (v1.1.1).
"""

import asyncio
import os
import sys
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest
from autonomy.email_triage.schemas import TriageCycleReport


def _make_report(**overrides) -> TriageCycleReport:
    """Build a TriageCycleReport with sensible defaults."""
    defaults = dict(
        cycle_id="test_cycle",
        started_at=1000.0,
        completed_at=1001.0,
        emails_fetched=5,
        emails_processed=5,
        tier_counts={1: 1, 3: 4},
        notifications_sent=1,
        notifications_suppressed=0,
        errors=[],
    )
    defaults.update(overrides)
    return TriageCycleReport(**defaults)


def _mock_lock_manager(acquired=True, fencing_token=42):
    """Build a mock DLM that yields (acquired, LockMetadata-like) from acquire_unified."""
    mgr = MagicMock()
    mock_meta = MagicMock()
    mock_meta.fencing_token = fencing_token

    @asynccontextmanager
    async def _acquire_unified(*args, **kwargs):
        yield (acquired, mock_meta if acquired else None)

    mgr.acquire_unified = _acquire_unified
    return mgr


def _dlm_patch(acquired=True, fencing_token=42):
    """Return a patch context manager for get_lock_manager."""
    mock_mgr = _mock_lock_manager(acquired=acquired, fencing_token=fencing_token)
    return patch(
        "core.distributed_lock_manager.get_lock_manager",
        new_callable=AsyncMock,
        return_value=mock_mgr,
    )


class TestAgentRuntimeTriageWiring:
    """_maybe_run_email_triage captures and logs the cycle report."""

    @pytest.mark.asyncio
    async def test_run_cycle_called_when_enabled(self):
        """When enabled and cooldown elapsed, run_cycle is called."""
        mock_report = _make_report()
        mock_runner = MagicMock()
        mock_runner.run_cycle = AsyncMock(return_value=mock_report)

        from autonomy.agent_runtime import UnifiedAgentRuntime

        runtime = object.__new__(UnifiedAgentRuntime)
        runtime._last_email_triage_run = 0.0

        with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}):
            with patch(
                "autonomy.email_triage.runner.EmailTriageRunner.get_instance",
                return_value=mock_runner,
            ):
                with _dlm_patch():
                    await UnifiedAgentRuntime._maybe_run_email_triage(runtime)

        mock_runner.run_cycle.assert_called_once()

    @pytest.mark.asyncio
    async def test_report_logged_when_not_skipped(self):
        """Non-skipped report triggers info-level logging."""
        mock_report = _make_report(emails_fetched=3, emails_processed=3)
        mock_runner = MagicMock()
        mock_runner.run_cycle = AsyncMock(return_value=mock_report)

        from autonomy.agent_runtime import UnifiedAgentRuntime

        runtime = object.__new__(UnifiedAgentRuntime)
        runtime._last_email_triage_run = 0.0

        with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}):
            with patch(
                "autonomy.email_triage.runner.EmailTriageRunner.get_instance",
                return_value=mock_runner,
            ):
                with _dlm_patch():
                    with patch("autonomy.agent_runtime.logger") as mock_logger:
                        await UnifiedAgentRuntime._maybe_run_email_triage(runtime)

                        # Should have logged the report summary
                        mock_logger.info.assert_called()
                        call_args = mock_logger.info.call_args
                        log_msg = call_args[0][0]
                        assert "Email triage" in log_msg

    @pytest.mark.asyncio
    async def test_skipped_report_not_logged(self):
        """Skipped report (disabled config) does NOT trigger info log."""
        mock_report = _make_report(skipped=True, skip_reason="disabled")
        mock_runner = MagicMock()
        mock_runner.run_cycle = AsyncMock(return_value=mock_report)

        from autonomy.agent_runtime import UnifiedAgentRuntime

        runtime = object.__new__(UnifiedAgentRuntime)
        runtime._last_email_triage_run = 0.0

        with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}):
            with patch(
                "autonomy.email_triage.runner.EmailTriageRunner.get_instance",
                return_value=mock_runner,
            ):
                with _dlm_patch():
                    with patch("autonomy.agent_runtime.logger") as mock_logger:
                        await UnifiedAgentRuntime._maybe_run_email_triage(runtime)

                        # info should NOT be called with "Email triage" for skipped reports
                        for call in mock_logger.info.call_args_list:
                            assert "Email triage" not in call[0][0]

    @pytest.mark.asyncio
    async def test_disabled_flag_skips_entirely(self):
        """When EMAIL_TRIAGE_ENABLED=false, run_cycle is never called."""
        mock_runner = MagicMock()
        mock_runner.run_cycle = AsyncMock()

        from autonomy.agent_runtime import UnifiedAgentRuntime

        runtime = object.__new__(UnifiedAgentRuntime)
        runtime._last_email_triage_run = 0.0

        with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "false"}):
            with patch(
                "autonomy.email_triage.runner.EmailTriageRunner.get_instance",
                return_value=mock_runner,
            ):
                await UnifiedAgentRuntime._maybe_run_email_triage(runtime)

        mock_runner.run_cycle.assert_not_called()

    @pytest.mark.asyncio
    async def test_cooldown_prevents_rapid_calls(self):
        """Second call within cooldown interval is skipped."""
        mock_report = _make_report()
        mock_runner = MagicMock()
        mock_runner.run_cycle = AsyncMock(return_value=mock_report)

        from autonomy.agent_runtime import UnifiedAgentRuntime

        runtime = object.__new__(UnifiedAgentRuntime)
        runtime._last_email_triage_run = time.monotonic()  # Just ran

        with patch.dict(os.environ, {
            "EMAIL_TRIAGE_ENABLED": "true",
            "EMAIL_TRIAGE_POLL_INTERVAL_S": "60",
        }):
            with patch(
                "autonomy.email_triage.runner.EmailTriageRunner.get_instance",
                return_value=mock_runner,
            ):
                await UnifiedAgentRuntime._maybe_run_email_triage(runtime)

        mock_runner.run_cycle.assert_not_called()

    @pytest.mark.asyncio
    async def test_timeout_handled_gracefully(self):
        """TimeoutError from run_cycle is caught and logged as warning."""
        mock_runner = MagicMock()
        mock_runner.run_cycle = AsyncMock(side_effect=asyncio.TimeoutError())

        from autonomy.agent_runtime import UnifiedAgentRuntime

        runtime = object.__new__(UnifiedAgentRuntime)
        runtime._last_email_triage_run = 0.0

        with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}):
            with patch(
                "autonomy.email_triage.runner.EmailTriageRunner.get_instance",
                return_value=mock_runner,
            ):
                with _dlm_patch():
                    # Should not raise
                    await UnifiedAgentRuntime._maybe_run_email_triage(runtime)

    @pytest.mark.asyncio
    async def test_exception_handled_gracefully(self):
        """General exception from run_cycle is caught (fail-closed default)."""
        mock_runner = MagicMock()
        mock_runner.run_cycle = AsyncMock(
            side_effect=RuntimeError("unexpected failure"),
        )

        from autonomy.agent_runtime import UnifiedAgentRuntime

        runtime = object.__new__(UnifiedAgentRuntime)
        runtime._last_email_triage_run = 0.0

        with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}):
            with patch(
                "autonomy.email_triage.runner.EmailTriageRunner.get_instance",
                return_value=mock_runner,
            ):
                with _dlm_patch():
                    # Should not raise
                    await UnifiedAgentRuntime._maybe_run_email_triage(runtime)


class TestSingleLeaderGuard:
    """v1.1.1: DLM-based single-leader guard for email triage."""

    @pytest.mark.asyncio
    async def test_lock_acquired_before_run_cycle(self):
        """DLM yields (True, meta) -> run_cycle is called once."""
        mock_report = _make_report()
        mock_runner = MagicMock()
        mock_runner.run_cycle = AsyncMock(return_value=mock_report)

        from autonomy.agent_runtime import UnifiedAgentRuntime

        runtime = object.__new__(UnifiedAgentRuntime)
        runtime._last_email_triage_run = 0.0

        with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}):
            with patch(
                "autonomy.email_triage.runner.EmailTriageRunner.get_instance",
                return_value=mock_runner,
            ):
                with _dlm_patch(acquired=True, fencing_token=99):
                    await UnifiedAgentRuntime._maybe_run_email_triage(runtime)

        mock_runner.run_cycle.assert_called_once()

    @pytest.mark.asyncio
    async def test_lock_not_acquired_skips_cycle(self):
        """DLM yields (False, None) -> run_cycle is NOT called."""
        mock_runner = MagicMock()
        mock_runner.run_cycle = AsyncMock()

        from autonomy.agent_runtime import UnifiedAgentRuntime

        runtime = object.__new__(UnifiedAgentRuntime)
        runtime._last_email_triage_run = 0.0

        with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}):
            with patch(
                "autonomy.email_triage.runner.EmailTriageRunner.get_instance",
                return_value=mock_runner,
            ):
                with _dlm_patch(acquired=False):
                    await UnifiedAgentRuntime._maybe_run_email_triage(runtime)

        mock_runner.run_cycle.assert_not_called()

    @pytest.mark.asyncio
    async def test_lock_ttl_matches_cycle_timeout_plus_headroom(self):
        """acquire_unified is called with ttl = timeout + 10."""
        mock_report = _make_report()
        mock_runner = MagicMock()
        mock_runner.run_cycle = AsyncMock(return_value=mock_report)

        from autonomy.agent_runtime import UnifiedAgentRuntime

        runtime = object.__new__(UnifiedAgentRuntime)
        runtime._last_email_triage_run = 0.0

        captured_kwargs = {}
        mock_mgr = MagicMock()
        mock_meta = MagicMock()
        mock_meta.fencing_token = 1

        @asynccontextmanager
        async def _capture_acquire(*args, **kwargs):
            captured_kwargs.update(kwargs)
            yield (True, mock_meta)

        mock_mgr.acquire_unified = _capture_acquire

        with patch.dict(os.environ, {
            "EMAIL_TRIAGE_ENABLED": "true",
            "EMAIL_TRIAGE_CYCLE_TIMEOUT_S": "25",
        }):
            with patch(
                "autonomy.email_triage.runner.EmailTriageRunner.get_instance",
                return_value=mock_runner,
            ):
                with patch(
                    "core.distributed_lock_manager.get_lock_manager",
                    new_callable=AsyncMock,
                    return_value=mock_mgr,
                ):
                    await UnifiedAgentRuntime._maybe_run_email_triage(runtime)

        assert captured_kwargs.get("ttl") == 35.0  # 25 + 10

    @pytest.mark.asyncio
    async def test_dlm_failure_fail_closed_skips_cycle(self):
        """get_lock_manager raises -> run_cycle NOT called (fail-closed)."""
        mock_runner = MagicMock()
        mock_runner.run_cycle = AsyncMock()

        from autonomy.agent_runtime import UnifiedAgentRuntime

        runtime = object.__new__(UnifiedAgentRuntime)
        runtime._last_email_triage_run = 0.0

        with patch.dict(os.environ, {
            "EMAIL_TRIAGE_ENABLED": "true",
            "EMAIL_TRIAGE_DLM_FAIL_OPEN": "false",
        }):
            with patch(
                "autonomy.email_triage.runner.EmailTriageRunner.get_instance",
                return_value=mock_runner,
            ):
                with patch(
                    "core.distributed_lock_manager.get_lock_manager",
                    new_callable=AsyncMock,
                    side_effect=RuntimeError("DLM unavailable"),
                ):
                    # Should not raise
                    await UnifiedAgentRuntime._maybe_run_email_triage(runtime)

        mock_runner.run_cycle.assert_not_called()

    @pytest.mark.asyncio
    async def test_dlm_failure_fail_open_runs_unguarded(self):
        """get_lock_manager raises + FAIL_OPEN=true -> run_cycle called."""
        mock_report = _make_report()
        mock_runner = MagicMock()
        mock_runner.run_cycle = AsyncMock(return_value=mock_report)

        from autonomy.agent_runtime import UnifiedAgentRuntime

        runtime = object.__new__(UnifiedAgentRuntime)
        runtime._last_email_triage_run = 0.0

        with patch.dict(os.environ, {
            "EMAIL_TRIAGE_ENABLED": "true",
            "EMAIL_TRIAGE_DLM_FAIL_OPEN": "true",
        }):
            with patch(
                "autonomy.email_triage.runner.EmailTriageRunner.get_instance",
                return_value=mock_runner,
            ):
                with patch(
                    "core.distributed_lock_manager.get_lock_manager",
                    new_callable=AsyncMock,
                    side_effect=RuntimeError("DLM unavailable"),
                ):
                    await UnifiedAgentRuntime._maybe_run_email_triage(runtime)

        mock_runner.run_cycle.assert_called_once()
