"""Tests for SecurityReviewer, GraduationTracker, and PrimeMetricsPoller."""
import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.security_reviewer import (
    SecurityFinding,
    SecurityReviewer,
    SecurityReviewResult,
    SecurityVerdict,
)
from backend.core.ouroboros.governance.graduation_tracker import (
    GraduationState,
    GraduationTracker,
)


# ---------------------------------------------------------------------------
# SecurityReviewer
# ---------------------------------------------------------------------------

class TestSecurityVerdict:
    def test_enum_values(self):
        assert SecurityVerdict.PASS == "pass"
        assert SecurityVerdict.WARN == "warn"
        assert SecurityVerdict.BLOCK == "block"


class TestSecurityReviewer:
    @pytest.mark.asyncio
    async def test_disabled_returns_pass(self):
        reviewer = SecurityReviewer(prime_client=None)
        result = await reviewer.review({}, ["test.py"], "test")
        assert result.verdict == SecurityVerdict.PASS
        assert result.reviewer_brain == "none"

    @pytest.mark.asyncio
    async def test_pass_verdict(self):
        client = AsyncMock()
        client.generate = AsyncMock(return_value=MagicMock(
            content=json.dumps({
                "verdict": "pass",
                "findings": [],
                "summary": "No issues",
            }),
            source="mock_brain",
            tokens_used=50,
        ))
        reviewer = SecurityReviewer(prime_client=client)
        result = await reviewer.review(
            {"content": "x = 1"}, ["safe.py"], "simple change"
        )
        assert result.verdict == SecurityVerdict.PASS

    @pytest.mark.asyncio
    async def test_block_verdict(self):
        client = AsyncMock()
        client.generate = AsyncMock(return_value=MagicMock(
            content=json.dumps({
                "verdict": "block",
                "findings": [{
                    "severity": "critical",
                    "category": "command_injection",
                    "file_path": "handler.py",
                    "line_number": 42,
                    "description": "Unsafe shell execution with user input",
                    "recommendation": "Use subprocess.run with shell=False",
                }],
                "summary": "Critical injection vulnerability",
            }),
            source="claude_api",
            tokens_used=200,
        ))
        reviewer = SecurityReviewer(prime_client=client)
        result = await reviewer.review(
            {"content": "# unsafe code"}, ["handler.py"], "handle user command"
        )
        assert result.verdict == SecurityVerdict.BLOCK
        assert len(result.findings) == 1
        assert result.findings[0].category == "command_injection"

    @pytest.mark.asyncio
    async def test_error_returns_pass(self):
        client = AsyncMock()
        client.generate = AsyncMock(side_effect=RuntimeError("boom"))
        reviewer = SecurityReviewer(prime_client=client)
        result = await reviewer.review({}, ["test.py"], "test")
        assert result.verdict == SecurityVerdict.PASS
        assert "boom" in result.summary

    def test_format_for_approval_pass(self):
        result = SecurityReviewResult(
            verdict=SecurityVerdict.PASS, findings=[], summary="clean",
            reviewer_brain="test", review_duration_s=1.0,
        )
        assert "PASS" in result.format_for_approval()

    def test_format_for_approval_block(self):
        result = SecurityReviewResult(
            verdict=SecurityVerdict.BLOCK,
            findings=[SecurityFinding(
                severity="critical", category="injection",
                file_path="x.py", line_number=10,
                description="bad", recommendation="fix",
            )],
            summary="bad", reviewer_brain="test", review_duration_s=1.0,
        )
        text = result.format_for_approval()
        assert "BLOCK" in text
        assert "injection" in text


# ---------------------------------------------------------------------------
# GraduationTracker
# ---------------------------------------------------------------------------

class TestGraduationState:
    def test_defaults(self):
        s = GraduationState()
        assert s.current_level == 2
        assert s.consecutive_successes == 0


class TestGraduationTracker:
    def test_initial_level(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = GraduationTracker(persistence_dir=Path(tmpdir))
            assert tracker.current_level == 2

    def test_consecutive_successes_track(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = GraduationTracker(persistence_dir=Path(tmpdir))
            for i in range(10):
                tracker.record_operation_outcome(f"op-{i}", success=True)
            assert tracker.state.consecutive_successes == 10

    def test_rollback_resets_consecutive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = GraduationTracker(persistence_dir=Path(tmpdir))
            for i in range(5):
                tracker.record_operation_outcome(f"op-{i}", success=True)
            tracker.record_operation_outcome("op-fail", success=False, rolled_back=True)
            assert tracker.state.consecutive_successes == 0
            assert tracker.state.total_rollbacks == 1

    def test_level_3_gate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = GraduationTracker(persistence_dir=Path(tmpdir))
            for i in range(20):
                tracker.record_operation_outcome(
                    f"op-{i}", success=True, proactive=True, proposal_accepted=True
                )
            assert tracker.current_level >= 3

    def test_level_3_not_reached_low_acceptance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = GraduationTracker(persistence_dir=Path(tmpdir))
            for i in range(20):
                tracker.record_operation_outcome(
                    f"op-{i}", success=True, proactive=True,
                    proposal_accepted=(i < 10),
                )
            assert tracker.current_level == 2

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker1 = GraduationTracker(persistence_dir=Path(tmpdir))
            for i in range(5):
                tracker1.record_operation_outcome(f"op-{i}", success=True)
            tracker2 = GraduationTracker(persistence_dir=Path(tmpdir))
            assert tracker2.state.consecutive_successes == 5

    def test_level_never_decreases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = GraduationTracker(persistence_dir=Path(tmpdir))
            tracker._state.current_level = 3
            tracker._save()
            tracker.record_operation_outcome("op-fail", success=False, rolled_back=True)
            assert tracker.current_level >= 3

    def test_health_dict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = GraduationTracker(persistence_dir=Path(tmpdir))
            h = tracker.health()
            assert "current_level" in h
            assert "consecutive_successes" in h


# ---------------------------------------------------------------------------
# PrimeMetricsPoller
# ---------------------------------------------------------------------------

class TestPrimeMetricsPoller:
    def test_disabled_without_endpoint(self):
        from backend.core.topology.prime_metrics_poller import PrimeMetricsPoller
        verifier = MagicMock()
        with patch.dict("os.environ", {"JARVIS_PRIME_URL": ""}):
            poller = PrimeMetricsPoller(verifier=verifier, endpoint="")
            assert poller.is_enabled is False

    def test_enabled_with_endpoint(self):
        from backend.core.topology.prime_metrics_poller import PrimeMetricsPoller
        verifier = MagicMock()
        poller = PrimeMetricsPoller(verifier=verifier, endpoint="http://10.0.0.5:8000")
        assert poller.is_enabled is True
        assert "/v1/metrics" in poller._endpoint

    @pytest.mark.asyncio
    async def test_start_stop_lifecycle(self):
        from backend.core.topology.prime_metrics_poller import PrimeMetricsPoller
        verifier = MagicMock()
        poller = PrimeMetricsPoller(
            verifier=verifier, endpoint="http://fake:8000", poll_interval_s=60.0
        )
        await poller.start()
        assert poller._task is not None
        await poller.stop()
        assert poller._task is None
