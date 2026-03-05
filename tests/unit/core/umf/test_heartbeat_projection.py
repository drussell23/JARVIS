"""Tests for the UMF heartbeat projection module.

Covers heartbeat ingestion, state retrieval, staleness detection,
unknown-subsystem handling, multi-subsystem aggregation, and
last-write-wins semantics.
"""
from __future__ import annotations

import time

import pytest

from backend.core.root_authority_types import SubsystemState
from backend.core.umf.heartbeat_projection import HeartbeatProjection
from backend.core.umf.types import (
    Kind,
    MessageSource,
    MessageTarget,
    Stream,
    UmfMessage,
)


def _make_heartbeat_msg(
    subsystem: str,
    state: str,
    liveness: bool = True,
    readiness: bool = True,
) -> UmfMessage:
    """Build a minimal UMF heartbeat message for testing."""
    source = MessageSource(
        repo="jarvis-ai-agent",
        component=subsystem,
        instance_id="inst-001",
        session_id="sess-test",
    )
    target = MessageTarget(repo="jarvis-ai-agent", component="supervisor")
    payload = {
        "subsystem_role": subsystem,
        "liveness": liveness,
        "readiness": readiness,
        "state": state,
        "last_error_code": None,
        "queue_depth": 0,
        "resource_pressure": 0.0,
    }
    return UmfMessage(
        stream=Stream.lifecycle,
        kind=Kind.heartbeat,
        source=source,
        target=target,
        payload=payload,
    )


class TestHeartbeatProjection:
    """Six tests covering heartbeat projection behaviour."""

    def test_ingest_heartbeat_updates_state(self):
        proj = HeartbeatProjection()
        msg = _make_heartbeat_msg("audio_engine", "ready")
        proj.ingest(msg)

        state = proj.get_state("audio_engine")
        assert state is not None
        assert state["state"] == "ready"
        assert state["liveness"] is True
        assert state["readiness"] is True

    def test_subsystem_state_maps_to_enum(self):
        proj = HeartbeatProjection()
        msg = _make_heartbeat_msg("vision", "degraded")
        proj.ingest(msg)

        state = proj.get_state("vision")
        assert state is not None
        assert state["state"] == SubsystemState.DEGRADED.value

    def test_stale_heartbeat_marks_degraded(self):
        proj = HeartbeatProjection(stale_timeout_s=0.01)
        msg = _make_heartbeat_msg("tts_engine", "ready")
        proj.ingest(msg)

        time.sleep(0.02)

        stale = proj.get_stale_subsystems()
        assert "tts_engine" in stale

    def test_unknown_subsystem_returns_none(self):
        proj = HeartbeatProjection()
        assert proj.get_state("nonexistent") is None

    def test_get_all_states(self):
        proj = HeartbeatProjection()
        proj.ingest(_make_heartbeat_msg("audio_engine", "ready"))
        proj.ingest(_make_heartbeat_msg("vision", "alive"))

        all_states = proj.get_all_states()
        assert "audio_engine" in all_states
        assert "vision" in all_states
        assert len(all_states) == 2

    def test_contradictory_reports_last_wins(self):
        proj = HeartbeatProjection()
        proj.ingest(_make_heartbeat_msg("audio_engine", "ready"))
        proj.ingest(_make_heartbeat_msg("audio_engine", "degraded"))

        state = proj.get_state("audio_engine")
        assert state is not None
        assert state["state"] == "degraded"
