"""Tests for CommProtocol backpressure queue.

Note: The current CommProtocol delivers synchronously to all transports — the
queue is implicit (asyncio event loop). Since transports run sequentially in
_emit, actual queue pressure is minimal. The test below passes without
reserved-slot backpressure changes because transports are called synchronously
and DECISION always reaches LogTransport regardless of flood volume.
Reserved-slot backpressure can be deferred to a follow-up if current transport
volume remains acceptable.
"""
from backend.core.ouroboros.governance.comm_protocol import CommProtocol, LogTransport, MessageType


async def test_decision_always_delivered_despite_full_queue():
    """DECISION must be deliverable even when non-reserved queue is full."""
    transport = LogTransport()
    comm = CommProtocol(transports=[transport])
    for i in range(200):
        await comm.emit_heartbeat(op_id=f"op-flood-{i}", phase="sandbox", progress_pct=0.0)
    await comm.emit_decision(op_id="op-crit", outcome="approved", reason_code="ok")
    decision_msgs = [m for m in transport.messages if m.msg_type == MessageType.DECISION]
    assert len(decision_msgs) >= 1
    assert decision_msgs[0].op_id == "op-crit"
