from __future__ import annotations

import time

import pytest

from backend.core.ouroboros.battle_test.live_dashboard import (
    DashboardTransport,
    LiveDashboard,
)
from backend.core.ouroboros.governance.comm_protocol import CommMessage, MessageType


def _msg(
    msg_type: MessageType,
    *,
    op_id: str = "op-019e0000-feed-7000-a111-deadbeef0001",
    seq: int = 1,
    payload: dict | None = None,
) -> CommMessage:
    return CommMessage(
        msg_type=msg_type,
        op_id=op_id,
        seq=seq,
        causal_parent_seq=None,
        payload=payload or {},
        timestamp=time.time(),
    )


@pytest.mark.asyncio
async def test_dashboard_transport_logs_tool_start_heartbeats() -> None:
    dashboard = LiveDashboard(session_id="bt-test")
    transport = DashboardTransport(dashboard)
    op_id = "op-019e0000-feed-7000-a111-deadbeef0001"

    await transport.send(
        _msg(
            MessageType.INTENT,
            op_id=op_id,
            payload={
                "goal": "Patch telemetry gap",
                "target_files": ["backend/core/ouroboros/battle_test/live_dashboard.py"],
                "risk_tier": "SAFE_AUTO",
            },
        )
    )
    await transport.send(
        _msg(
            MessageType.HEARTBEAT,
            op_id=op_id,
            seq=2,
            payload={
                "phase": "generate",
                "tool_name": "read_file",
                "tool_args_summary": "backend/core/ouroboros/battle_test/live_dashboard.py",
                "round_index": 2,
                "tool_starting": True,
                "preamble": "Checking the existing dashboard transport before patching.",
            },
        )
    )

    events = list(dashboard._events)
    assert any("Checking the existing dashboard transport before patching." in event for event in events)
    assert any("T3" in event and "reading" in event and "live_dashboard.py" in event for event in events)


@pytest.mark.asyncio
async def test_dashboard_transport_tracks_route_cost_telemetry() -> None:
    dashboard = LiveDashboard(session_id="bt-test")
    transport = DashboardTransport(dashboard)
    op_id = "op-019e0000-feed-7000-a111-deadbeef0002"

    await transport.send(
        _msg(
            MessageType.INTENT,
            op_id=op_id,
            payload={
                "goal": "Fix urgent failure",
                "target_files": ["backend/core/ouroboros/governance/orchestrator.py"],
                "risk_tier": "SAFE_AUTO",
            },
        )
    )
    await transport.send(
        _msg(
            MessageType.DECISION,
            op_id=op_id,
            seq=2,
            payload={
                "outcome": "immediate",
                "reason_code": "urgency_route:critical_urgency:test_failure",
                "route": "immediate",
                "route_reason": "critical_urgency:test_failure",
                "budget_profile": "120s fast path",
                "details": {
                    "route": "immediate",
                    "route_description": "Claude direct",
                },
            },
        )
    )
    await transport.send(
        _msg(
            MessageType.HEARTBEAT,
            op_id=op_id,
            seq=3,
            payload={
                "phase": "cost",
                "route": "immediate",
                "provider": "claude",
                "cost_usd": 0.0125,
                "cost_event": "generation_attempt",
            },
        )
    )

    stats = dashboard._route_costs["immediate"]
    assert dashboard._op_routes[op_id] == "immediate"
    assert stats.total_usd == pytest.approx(0.0125)
    assert stats.charge_count == 1
    assert op_id in stats.op_ids
    assert any("IMMEDIATE" in event and "+$0.0125" in event for event in dashboard._events)


def test_route_cost_panel_renders_sparklines() -> None:
    dashboard = LiveDashboard(session_id="bt-test")
    dashboard.op_route("op-1", "standard", reason="default")
    dashboard.route_cost("op-1", 0.002, provider="doubleword", route="standard", event="generation_attempt")
    dashboard.route_cost("op-1", 0.006, provider="claude", route="standard", event="generation_attempt")

    panel = dashboard._build_route_cost_panel()
    rendered = panel.renderable.plain if hasattr(panel.renderable, "plain") else str(panel.renderable)

    assert "STD" in rendered
    assert "$0.0080" in rendered
    assert any(ch in rendered for ch in "▁▂▃▄▅▆▇█")
