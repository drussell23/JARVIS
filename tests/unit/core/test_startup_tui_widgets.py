"""Tests for TUI startup dashboard data bridge.

Disease 10 Wiring, Task 8.
"""
from __future__ import annotations

import time
import pytest

from backend.core.startup_telemetry import StartupEvent, StartupEventBus
from backend.core.supervisor_tui_bridge import (
    TUIBridge,
    InlineSummary,
    DetailSnapshot,
)


@pytest.fixture
def bridge() -> TUIBridge:
    return TUIBridge()


def _make_event(event_type: str, detail: dict, phase: str = None) -> StartupEvent:
    return StartupEvent(
        trace_id="test",
        event_type=event_type,
        timestamp=time.monotonic(),
        wall_clock="2026-03-06T12:00:00Z",
        authority_state="BOOT_POLICY_ACTIVE",
        phase=phase,
        detail=detail,
    )


class TestInlineSummary:

    async def test_phase_gate_updates_current_phase(self, bridge):
        evt = _make_event("phase_gate", {"status": "passed", "duration_s": 1.0}, phase="PREWARM_GCP")
        await bridge.consume(evt)
        summary = bridge.inline_summary
        assert summary.last_resolved_phase == "PREWARM_GCP"

    async def test_budget_acquire_updates_occupancy(self, bridge):
        evt = _make_event("budget_acquire", {
            "category": "MODEL_LOAD",
            "name": "prime",
            "queue_depth": 0,
            "hard_slot": True,
        })
        await bridge.consume(evt)
        summary = bridge.inline_summary
        assert summary.budget_active > 0

    async def test_lease_acquired_updates_status(self, bridge):
        evt = _make_event("lease_acquired", {
            "host": "10.0.0.1",
            "port": 8000,
            "lease_epoch": 1,
            "ttl_s": 120.0,
        })
        await bridge.consume(evt)
        summary = bridge.inline_summary
        assert summary.lease_status == "ACTIVE"

    async def test_authority_transition_updates_state(self, bridge):
        evt = StartupEvent(
            trace_id="test",
            event_type="authority_transition",
            timestamp=time.monotonic(),
            wall_clock="",
            authority_state="HANDOFF_PENDING",
            phase=None,
            detail={"from_state": "BOOT_POLICY_ACTIVE", "to_state": "HANDOFF_PENDING"},
        )
        await bridge.consume(evt)
        summary = bridge.inline_summary
        assert summary.authority_state == "HANDOFF_PENDING"


class TestDetailSnapshot:

    async def test_phase_timeline_accumulates(self, bridge):
        for phase in ["PREWARM_GCP", "CORE_SERVICES"]:
            evt = _make_event("phase_gate", {
                "status": "passed",
                "duration_s": 1.5,
            }, phase=phase)
            await bridge.consume(evt)
        detail = bridge.detail_snapshot
        assert len(detail.phase_timeline) == 2
        assert detail.phase_timeline[0]["phase"] == "PREWARM_GCP"

    async def test_budget_contention_tracked(self, bridge):
        evt = _make_event("budget_acquire", {
            "category": "MODEL_LOAD",
            "name": "prime",
            "wait_s": 3.2,
            "queue_depth": 1,
            "hard_slot": True,
        })
        await bridge.consume(evt)
        evt2 = _make_event("budget_release", {
            "category": "MODEL_LOAD",
            "name": "prime",
            "held_s": 12.3,
        })
        await bridge.consume(evt2)
        detail = bridge.detail_snapshot
        assert len(detail.budget_entries) >= 1

    async def test_handoff_trace_recorded(self, bridge):
        evt = _make_event("authority_transition", {
            "from_state": "BOOT_POLICY_ACTIVE",
            "to_state": "HANDOFF_PENDING",
            "guards_checked": 3,
            "duration_ms": 12,
        })
        await bridge.consume(evt)
        detail = bridge.detail_snapshot
        assert len(detail.handoff_trace) == 1
