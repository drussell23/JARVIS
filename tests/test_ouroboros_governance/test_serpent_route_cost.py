from __future__ import annotations

import time

import pytest

from backend.core.ouroboros.battle_test.serpent_flow import (
    SerpentFlow,
    SerpentTransport,
)
from backend.core.ouroboros.governance.comm_protocol import CommMessage, MessageType


def _msg(
    msg_type: MessageType,
    *,
    op_id: str = "op-019e0000-feed-7000-a111-deadbeef0099",
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
async def test_serpent_transport_tracks_route_cost_telemetry() -> None:
    flow = SerpentFlow()
    transport = SerpentTransport(flow)
    op_id = "op-019e0000-feed-7000-a111-deadbeef0099"

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
                "budget_profile": {"max_dw_wait_s": 0.0},
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

    assert flow._op_routes[op_id] == "immediate"
    assert flow._route_costs["immediate"]["total"] == pytest.approx(0.0125)
    assert op_id in flow._route_costs["immediate"]["ops"]
    assert "IMM $0.013" in flow._route_cost_toolbar_summary()
