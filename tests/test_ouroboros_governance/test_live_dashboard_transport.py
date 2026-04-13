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

