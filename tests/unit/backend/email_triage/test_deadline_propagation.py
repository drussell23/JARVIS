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
    runner._resolver = MagicMock()
    runner._state_store = None
    runner._label_map = {}
    runner._labels_initialized = True
    runner._fencing_token = 0
    runner._warmed_up = True
    runner._cold_start_done = True
    runner._outcome_collector = MagicMock()
    runner._outcome_collector.record = AsyncMock()
    runner._weight_adapter = None

    # Mock workspace agent to return one email
    mock_workspace = AsyncMock()
    mock_workspace.list_emails = AsyncMock(return_value=[
        {"id": "msg1", "from": "test@example.com", "subject": "Test", "snippet": "hi", "labelIds": []}
    ])
    runner._resolver.get = lambda name: {
        "workspace_agent": mock_workspace,
        "router": MagicMock(),
        "notifier": MagicMock(),
    }.get(name)

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

    with patch("autonomy.email_triage.runner.extract_features", side_effect=mock_extract):
        with patch("autonomy.email_triage.runner.score_email", return_value=MagicMock(tier=3, score=0.5, signals=[])):
            with patch("autonomy.email_triage.runner.apply_label", new_callable=AsyncMock):
                try:
                    await asyncio.wait_for(runner.run_cycle(deadline=deadline), timeout=5.0)
                except Exception as exc:
                    print(f"DEBUG: run_cycle raised: {type(exc).__name__}: {exc}")
                    import traceback; traceback.print_exc()

    assert captured_deadline is not None, "deadline was not propagated to extract_features"
    assert captured_deadline == deadline, f"Expected {deadline}, got {captured_deadline}"
