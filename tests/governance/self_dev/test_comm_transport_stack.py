"""Tests for CommProtocol transport ordering and idempotency keys."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.comm_protocol import CommProtocol, LogTransport


def test_log_transport_always_first_and_present():
    """LogTransport must be first and always present."""
    t = LogTransport()
    extra = MagicMock()
    extra.send = AsyncMock()
    comm = CommProtocol(transports=[t, extra])
    assert comm._transports[0] is t


def test_idempotency_key_format():
    """Each message must have idempotency_key = op_id:boot_id:phase:seq."""
    transport = LogTransport()
    comm = CommProtocol(transports=[transport])
    asyncio.get_event_loop().run_until_complete(
        comm.emit_intent(
            op_id="op-test", goal="g", target_files=[], risk_tier="SAFE_AUTO", blast_radius=1
        )
    )
    msg = transport.messages[0]
    parts = msg.idempotency_key.split(":")
    assert parts[0] == "op-test"
    assert len(parts) == 4  # op_id:boot_id:phase:seq


def test_boot_id_stable_across_emits():
    """All messages from same CommProtocol instance share the same boot_id."""
    transport = LogTransport()
    comm = CommProtocol(transports=[transport])
    asyncio.get_event_loop().run_until_complete(
        comm.emit_intent(op_id="op-a", goal="g", target_files=[], risk_tier="SAFE_AUTO", blast_radius=1)
    )
    asyncio.get_event_loop().run_until_complete(
        comm.emit_heartbeat(op_id="op-a", phase="sandbox", progress_pct=50.0)
    )
    key1 = transport.messages[0].idempotency_key
    key2 = transport.messages[1].idempotency_key
    boot_id_1 = key1.split(":")[1]
    boot_id_2 = key2.split(":")[1]
    assert boot_id_1 == boot_id_2


def test_transport_failure_does_not_block_log_transport():
    """Failing transport must not prevent LogTransport from receiving message."""
    good = LogTransport()
    bad = MagicMock()
    bad.send = AsyncMock(side_effect=RuntimeError("down"))
    comm = CommProtocol(transports=[good, bad])
    asyncio.get_event_loop().run_until_complete(
        comm.emit_intent(op_id="op-x", goal="g", target_files=[], risk_tier="SAFE_AUTO", blast_radius=1)
    )
    assert len(good.messages) == 1
