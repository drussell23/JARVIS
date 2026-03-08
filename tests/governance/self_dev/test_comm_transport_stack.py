"""Tests for CommProtocol transport ordering and idempotency keys."""
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.comm_protocol import CommProtocol, LogTransport


def test_log_transport_stays_first_when_passed_first():
    """When LogTransport is passed first, it remains first in the transport list."""
    t = LogTransport()
    extra = MagicMock()
    extra.send = AsyncMock()
    comm = CommProtocol(transports=[t, extra])
    assert comm._transports[0] is t


async def test_idempotency_key_format():
    """Each message must have idempotency_key = op_id:boot_id:phase:seq."""
    transport = LogTransport()
    comm = CommProtocol(transports=[transport])
    await comm.emit_intent(
        op_id="op-test", goal="g", target_files=[], risk_tier="SAFE_AUTO", blast_radius=1
    )
    msg = transport.messages[0]
    parts = msg.idempotency_key.split(":")
    assert parts[0] == "op-test"
    assert len(parts) == 4  # op_id:boot_id:phase:seq


async def test_boot_id_stable_across_emits():
    """All messages from same CommProtocol instance share the same boot_id."""
    transport = LogTransport()
    comm = CommProtocol(transports=[transport])
    await comm.emit_intent(op_id="op-a", goal="g", target_files=[], risk_tier="SAFE_AUTO", blast_radius=1)
    await comm.emit_heartbeat(op_id="op-a", phase="sandbox", progress_pct=50.0)
    key1 = transport.messages[0].idempotency_key
    key2 = transport.messages[1].idempotency_key
    boot_id_1 = key1.split(":")[1]
    boot_id_2 = key2.split(":")[1]
    assert boot_id_1 == boot_id_2


async def test_transport_failure_does_not_block_log_transport():
    """Failing transport must not prevent LogTransport from receiving message."""
    good = LogTransport()
    bad = MagicMock()
    bad.send = AsyncMock(side_effect=RuntimeError("down"))
    comm = CommProtocol(transports=[good, bad])
    await comm.emit_intent(op_id="op-x", goal="g", target_files=[], risk_tier="SAFE_AUTO", blast_radius=1)
    assert len(good.messages) == 1
