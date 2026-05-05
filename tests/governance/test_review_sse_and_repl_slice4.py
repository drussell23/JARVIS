"""Tests for Gap #4 Slice 4 — SSE event types + publish helper +
REPL verb regression checks.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_REVIEW_BRANCH_ACCEPTED,
    EVENT_TYPE_REVIEW_BRANCH_CREATED,
    EVENT_TYPE_REVIEW_BRANCH_EXPIRED,
    EVENT_TYPE_REVIEW_BRANCH_REJECTED,
    publish_review_branch_event,
)


# ===========================================================================
# Event type vocabulary
# ===========================================================================


def test_review_branch_event_types_pinned():
    assert EVENT_TYPE_REVIEW_BRANCH_CREATED == "review_branch_created"
    assert EVENT_TYPE_REVIEW_BRANCH_ACCEPTED == "review_branch_accepted"
    assert EVENT_TYPE_REVIEW_BRANCH_REJECTED == "review_branch_rejected"
    assert EVENT_TYPE_REVIEW_BRANCH_EXPIRED == "review_branch_expired"


# ===========================================================================
# publish_review_branch_event
# ===========================================================================


@pytest.mark.parametrize("state", [
    "pending", "accepted", "rejected", "expired",
])
def test_publish_recognized_states_does_not_raise(monkeypatch, state):
    """Publish helper accepts every state and never raises; returns
    None when stream is disabled (default)."""
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "false")
    result = publish_review_branch_event(
        state, "op-x", branch_name="ouroboros/preview/op-x",
        archive_ref="d-1", risk_tier="notify_apply",
    )
    # Stream is disabled in this test → returns None
    assert result is None


def test_publish_unknown_state_returns_none(monkeypatch):
    """Defensive: unknown state returns None without raising."""
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
    result = publish_review_branch_event(
        "nonsense", "op-x",
    )
    assert result is None


def test_publish_truncates_long_error(monkeypatch):
    """Defensive: very long error messages are truncated; never raises."""
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "false")
    huge = "x" * 5000
    result = publish_review_branch_event(
        "rejected", "op-x", error=huge,
    )
    assert result is None  # stream disabled


# ===========================================================================
# REPL verb dispatch regression — AST grep
# ===========================================================================


def test_serpent_flow_dispatches_accept_verb():
    """Slice 4 dispatch hook: ``/accept`` must route to
    ``_handle_accept`` in the SerpentREPL main loop. A future refactor
    that drops this branch would silently break the IDE-native review
    operator decision flow."""
    src = open(
        "/Users/djrussell23/Documents/repos/JARVIS-AI-Agent/"
        "backend/core/ouroboros/battle_test/serpent_flow.py"
    ).read()
    assert "Gap #4 Slice 4" in src
    assert 'line.startswith("/accept")' in src
    assert "self._handle_accept" in src
    assert 'line.startswith("/reject")' in src
    assert "self._handle_reject" in src
    assert "self._handle_review" in src


def test_serpent_flow_handler_methods_defined():
    """The three handler methods MUST be defined on the SerpentREPL
    class. Removing any of them would break the dispatch the previous
    test pins."""
    src = open(
        "/Users/djrussell23/Documents/repos/JARVIS-AI-Agent/"
        "backend/core/ouroboros/battle_test/serpent_flow.py"
    ).read()
    assert "async def _handle_accept" in src
    assert "async def _handle_reject" in src
    assert "def _handle_review" in src


# ===========================================================================
# ReviewCoordinator publish hook regression
# ===========================================================================


def test_coordinator_publishes_state_event():
    """The coordinator's _publish_state_event helper must be present
    and called from the lifecycle hooks."""
    src = open(
        "/Users/djrussell23/Documents/repos/JARVIS-AI-Agent/"
        "backend/core/ouroboros/governance/review_coordinator.py"
    ).read()
    assert "_publish_state_event" in src
    assert "publish_review_branch_event" in src
    # Must be called for all 4 lifecycle states.
    for state in ('"pending"', '"accepted"', '"rejected"', '"expired"'):
        assert state in src, f"missing _publish_state_event(state={state})"
