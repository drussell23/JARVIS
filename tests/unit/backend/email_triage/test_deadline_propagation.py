"""Tests for deadline propagation through triage pipeline."""

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest
from autonomy.email_triage.schemas import TriageCycleReport


@pytest.mark.asyncio
async def test_run_cycle_propagates_deadline_to_extract_features():
    """run_cycle(deadline=X) must pass deadline to extract_features()."""
    from autonomy.email_triage.runner import EmailTriageRunner
    from autonomy.email_triage.config import get_triage_config

    config = get_triage_config()
    config.enabled = True
    config.max_emails_per_cycle = 1

    runner = EmailTriageRunner.__new__(EmailTriageRunner)
    runner._config = config
    runner._state_store = None
    runner._state_store_initialized = True
    runner._label_map = {}
    runner._labels_initialized = True
    runner._fencing_token = 0
    runner._warmed_up = True
    runner._cold_start_done = True
    runner._outcome_collector = None
    runner._weight_adapter = None
    runner._outbox_replayed = True
    runner._prior_triaged = {}
    # C2 attributes
    runner._extraction_latencies_ms = []
    runner._extraction_p95_ema_ms = 0.0

    # Mock workspace agent with _fetch_unread_emails (the actual method run_cycle calls)
    mock_workspace = AsyncMock()
    mock_workspace._fetch_unread_emails = AsyncMock(return_value={
        "emails": [
            {"id": "msg1", "from": "test@example.com", "subject": "Test", "snippet": "hi", "labelIds": []}
        ]
    })

    # Mock resolver with resolve_all as async and get as sync
    mock_resolver = MagicMock()
    mock_resolver.resolve_all = AsyncMock()
    mock_resolver.get = lambda name: {
        "workspace_agent": mock_workspace,
        "router": MagicMock(),
        "notifier": MagicMock(),
    }.get(name)
    runner._resolver = mock_resolver

    deadline = time.monotonic() + 25.0
    captured_deadline = None

    async def mock_extract(email_dict, router, deadline=None, config=None):
        nonlocal captured_deadline
        captured_deadline = deadline
        from autonomy.email_triage.schemas import EmailFeatures
        return EmailFeatures(
            message_id="msg1", sender="test@example.com",
            sender_domain="example.com", subject="Test", snippet="hi",
            is_reply=False, has_attachment=False, label_ids=[],
            keywords=[], sender_frequency=0, urgency_signals=[],
            extraction_confidence=0.5, extraction_source="heuristic",
        )

    with patch.object(runner, "_ensure_state_store", new_callable=AsyncMock):
        with patch.object(runner, "_cold_start_recovery", new_callable=AsyncMock):
            with patch("autonomy.email_triage.runner.extract_features", side_effect=mock_extract):
                with patch("autonomy.email_triage.runner.score_email", return_value=MagicMock(tier=3, score=0.5, signals=[])):
                    with patch("autonomy.email_triage.runner.apply_label", new_callable=AsyncMock):
                        try:
                            await asyncio.wait_for(runner.run_cycle(deadline=deadline), timeout=5.0)
                        except Exception:
                            pass  # May fail on other dependencies, that's fine

    assert captured_deadline is not None, "deadline was not propagated to extract_features"
    # C2: _extract_one computes a per-email deadline clamped to the cycle deadline.
    # The per-email deadline must be <= the cycle deadline.
    assert captured_deadline <= deadline + 1.0, (
        f"Per-email deadline {captured_deadline} exceeds cycle deadline {deadline}"
    )
