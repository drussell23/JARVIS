"""Tests for Phase B decision-action bridge wiring in runner."""

import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest

from autonomy.email_triage.config import TriageConfig
from autonomy.email_triage.runner import EmailTriageRunner


def _make_runner(config=None):
    """Build a runner with minimal mocks for testing."""
    if config is None:
        config = TriageConfig(enabled=True, max_emails_per_cycle=5)

    runner = EmailTriageRunner.__new__(EmailTriageRunner)
    runner._config = config
    runner._state_store = None
    runner._state_store_initialized = True
    runner._label_map = {}
    runner._labels_initialized = True
    runner._current_fencing_token = 0
    runner._last_committed_fencing_token = 0
    runner._warmed_up = True
    runner._cold_start_recovered = True
    runner._outcome_collector = None
    runner._weight_adapter = None
    runner._outbox_replayed = True
    runner._prior_triaged = {}
    runner._extraction_latencies_ms = []
    runner._extraction_p95_ema_ms = 0.0
    runner._last_report = None
    runner._last_report_at = 0.0
    runner._triaged_emails = {}
    runner._committed_snapshot = None
    runner._triage_schema_version = "1.0"

    import asyncio
    runner._report_lock = asyncio.Lock()

    # Phase B contracts
    from core.contracts.decision_envelope import EnvelopeFactory
    from autonomy.contracts.behavioral_health import BehavioralHealthMonitor
    from autonomy.email_triage.triage_policy_gate import TriagePolicyGate
    from autonomy.email_triage.policy import NotificationPolicy

    runner._envelope_factory = EnvelopeFactory()
    runner._health_monitor = BehavioralHealthMonitor()
    runner._commit_ledger = None
    runner._policy = NotificationPolicy(config)
    runner._policy_gate = TriagePolicyGate(runner._policy, config)
    runner._runner_id = "runner-test"

    # Mock resolver
    mock_resolver = MagicMock()
    mock_workspace = AsyncMock()
    mock_workspace._fetch_unread_emails = AsyncMock(return_value={"emails": []})
    mock_resolver.resolve_all = AsyncMock()
    mock_resolver.get = lambda name: {
        "workspace_agent": mock_workspace,
        "router": MagicMock(),
        "notifier": MagicMock(),
    }.get(name)
    runner._resolver = mock_resolver

    return runner


def _fake_features(message_id="msg-1"):
    """Build a minimal EmailFeatures for tests."""
    from autonomy.email_triage.schemas import EmailFeatures
    return EmailFeatures(
        message_id=message_id, sender="user@example.com",
        sender_domain="example.com", subject="Test",
        snippet="hi", is_reply=False, has_attachment=False,
        label_ids=(), keywords=(), sender_frequency="occasional",
        urgency_signals=(), extraction_confidence=0.8,
        extraction_source="heuristic",
    )


def _fake_scoring():
    """Build a minimal scoring mock."""
    return MagicMock(
        tier=3, score=40, tier_label="jarvis/tier3_review",
        breakdown={}, idempotency_key="idem-1",
        scoring_explanation="test",
    )


class TestThrottleCheck:
    @pytest.mark.asyncio
    async def test_circuit_break_skips_cycle(self):
        runner = _make_runner()
        from autonomy.contracts.behavioral_health import ThrottleRecommendation
        runner._health_monitor.should_throttle = lambda: (
            ThrottleRecommendation.CIRCUIT_BREAK, "test anomaly"
        )
        report = await runner.run_cycle()
        assert report.skipped is True
        assert "circuit_break" in report.skip_reason

    @pytest.mark.asyncio
    async def test_pause_skips_cycle(self):
        runner = _make_runner()
        from autonomy.contracts.behavioral_health import ThrottleRecommendation
        runner._health_monitor.should_throttle = lambda: (
            ThrottleRecommendation.PAUSE_CYCLE, "error rate"
        )
        report = await runner.run_cycle()
        assert report.skipped is True
        assert "pause" in report.skip_reason

    @pytest.mark.asyncio
    async def test_healthy_proceeds(self):
        runner = _make_runner()
        # Default health monitor returns NONE — cycle should proceed normally
        report = await runner.run_cycle()
        assert report.skipped is False


class TestEnvelopeCreation:
    @pytest.mark.asyncio
    async def test_envelopes_created_for_each_email(self):
        runner = _make_runner()
        mock_ws = runner._resolver.get("workspace_agent")
        mock_ws._fetch_unread_emails = AsyncMock(return_value={
            "emails": [
                {"id": "msg-1", "from": "a@example.com", "subject": "Test 1", "snippet": "hi", "labelIds": []},
                {"id": "msg-2", "from": "b@example.com", "subject": "Test 2", "snippet": "hello", "labelIds": []},
            ]
        })

        with patch("autonomy.email_triage.runner.extract_features") as mock_extract:
            def fake_extract(email, router, deadline=None, config=None):
                mid = email.get("id", "?")
                return _fake_features(mid)
            mock_extract.side_effect = fake_extract

            with patch("autonomy.email_triage.runner.score_email") as mock_score:
                mock_score.return_value = _fake_scoring()
                with patch.object(runner, "_apply_label", new_callable=AsyncMock):
                    report = await runner.run_cycle()

        assert report.emails_processed == 2


class TestHealthRecording:
    @pytest.mark.asyncio
    async def test_health_monitor_records_cycle(self):
        runner = _make_runner()
        mock_ws = runner._resolver.get("workspace_agent")
        mock_ws._fetch_unread_emails = AsyncMock(return_value={"emails": []})

        # Spy on record_cycle
        original_record = runner._health_monitor.record_cycle
        recorded = []
        def spy_record(report, envelopes):
            recorded.append((report, envelopes))
            return original_record(report, envelopes)
        runner._health_monitor.record_cycle = spy_record

        await runner.run_cycle()
        assert len(recorded) == 1
        report, envelopes = recorded[0]
        assert report is not None


class TestLedgerIntegration:
    @pytest.mark.asyncio
    async def test_ledger_reserve_commit_on_success(self, tmp_path):
        from core.contracts.action_commit_ledger import ActionCommitLedger, CommitState
        config = TriageConfig(enabled=True, max_emails_per_cycle=1)
        runner = _make_runner(config)

        ledger = ActionCommitLedger(tmp_path / "test.db")
        await ledger.start()
        runner._commit_ledger = ledger

        mock_ws = runner._resolver.get("workspace_agent")
        mock_ws._fetch_unread_emails = AsyncMock(return_value={
            "emails": [
                {"id": "msg-1", "from": "a@test.com", "subject": "Urgent", "snippet": "now", "labelIds": []},
            ]
        })

        with patch("autonomy.email_triage.runner.extract_features") as mock_extract:
            mock_extract.return_value = _fake_features("msg-1")
            with patch("autonomy.email_triage.runner.score_email") as mock_score:
                mock_score.return_value = _fake_scoring()
                with patch.object(runner, "_apply_label", new_callable=AsyncMock):
                    await runner.run_cycle()

        records = await ledger.query(since_epoch=0)
        assert len(records) == 1
        assert records[0].state == CommitState.COMMITTED
        assert records[0].outcome == "success"
        await ledger.stop()

    @pytest.mark.asyncio
    async def test_ledger_abort_on_label_failure(self, tmp_path):
        from core.contracts.action_commit_ledger import ActionCommitLedger, CommitState
        config = TriageConfig(enabled=True, max_emails_per_cycle=1)
        runner = _make_runner(config)

        ledger = ActionCommitLedger(tmp_path / "test.db")
        await ledger.start()
        runner._commit_ledger = ledger

        mock_ws = runner._resolver.get("workspace_agent")
        mock_ws._fetch_unread_emails = AsyncMock(return_value={
            "emails": [
                {"id": "msg-1", "from": "a@test.com", "subject": "Test", "snippet": "hi", "labelIds": []},
            ]
        })

        with patch("autonomy.email_triage.runner.extract_features") as mock_extract:
            mock_extract.return_value = _fake_features("msg-1")
            with patch("autonomy.email_triage.runner.score_email") as mock_score:
                mock_score.return_value = _fake_scoring()
                with patch.object(runner, "_apply_label", new_callable=AsyncMock,
                                  side_effect=Exception("Gmail error")):
                    await runner.run_cycle()

        records = await ledger.query(since_epoch=0)
        assert len(records) == 1
        assert records[0].state == CommitState.ABORTED
        assert "action_failed" in (records[0].abort_reason or "")
        await ledger.stop()

    @pytest.mark.asyncio
    async def test_fail_closed_on_reserve_failure(self, tmp_path):
        from core.contracts.action_commit_ledger import ActionCommitLedger
        config = TriageConfig(enabled=True, max_emails_per_cycle=1)
        runner = _make_runner(config)

        ledger = ActionCommitLedger(tmp_path / "test.db")
        await ledger.start()
        runner._commit_ledger = ledger
        # Break reserve to simulate failure
        ledger.reserve = AsyncMock(side_effect=Exception("DB locked"))

        mock_ws = runner._resolver.get("workspace_agent")
        mock_ws._fetch_unread_emails = AsyncMock(return_value={
            "emails": [
                {"id": "msg-1", "from": "a@test.com", "subject": "Test", "snippet": "hi", "labelIds": []},
            ]
        })

        label_mock = AsyncMock()
        with patch("autonomy.email_triage.runner.extract_features") as mock_extract:
            mock_extract.return_value = _fake_features("msg-1")
            with patch("autonomy.email_triage.runner.score_email") as mock_score:
                mock_score.return_value = _fake_scoring()
                with patch.object(runner, "_apply_label", label_mock):
                    report = await runner.run_cycle()

        # Label should NOT have been called (fail-closed)
        label_mock.assert_not_called()
        assert any("ledger_reserve" in e for e in report.errors)
        await ledger.stop()


class TestLedgerNoneFailClosed:
    @pytest.mark.asyncio
    async def test_no_ledger_with_persistence_enabled_blocks_actions(self):
        """When persistence is enabled but ledger is None (init failed),
        actions must be blocked (fail-closed)."""
        config = TriageConfig(enabled=True, max_emails_per_cycle=1,
                              state_persistence_enabled=True)
        runner = _make_runner(config)
        # Ledger is None by default in _make_runner
        assert runner._commit_ledger is None

        mock_ws = runner._resolver.get("workspace_agent")
        mock_ws._fetch_unread_emails = AsyncMock(return_value={
            "emails": [
                {"id": "msg-1", "from": "a@test.com", "subject": "Test", "snippet": "hi", "labelIds": []},
            ]
        })

        label_mock = AsyncMock()
        with patch("autonomy.email_triage.runner.extract_features") as mock_extract:
            mock_extract.return_value = _fake_features("msg-1")
            with patch("autonomy.email_triage.runner.score_email") as mock_score:
                mock_score.return_value = _fake_scoring()
                with patch.object(runner, "_apply_label", label_mock):
                    report = await runner.run_cycle()

        # Action blocked — label never called
        label_mock.assert_not_called()
        assert any("no_ledger" in e for e in report.errors)

    @pytest.mark.asyncio
    async def test_no_ledger_with_persistence_disabled_allows_actions(self):
        """When persistence is explicitly disabled, actions proceed
        without a ledger (user opted out)."""
        config = TriageConfig(enabled=True, max_emails_per_cycle=1,
                              state_persistence_enabled=False)
        runner = _make_runner(config)
        assert runner._commit_ledger is None

        mock_ws = runner._resolver.get("workspace_agent")
        mock_ws._fetch_unread_emails = AsyncMock(return_value={
            "emails": [
                {"id": "msg-1", "from": "a@test.com", "subject": "Test", "snippet": "hi", "labelIds": []},
            ]
        })

        label_mock = AsyncMock()
        with patch("autonomy.email_triage.runner.extract_features") as mock_extract:
            mock_extract.return_value = _fake_features("msg-1")
            with patch("autonomy.email_triage.runner.score_email") as mock_score:
                mock_score.return_value = _fake_scoring()
                with patch.object(runner, "_apply_label", label_mock):
                    report = await runner.run_cycle()

        # Action allowed — label was called
        label_mock.assert_called_once()
        assert not any("no_ledger" in e for e in report.errors)
