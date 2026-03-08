"""Test that _build_comm_protocol wires all transports in fixed order."""
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.integration import _build_comm_protocol
from backend.core.ouroboros.governance.comm_protocol import CommProtocol, LogTransport


def test_build_comm_protocol_returns_comm_protocol():
    comm = _build_comm_protocol(config=None)
    assert isinstance(comm, CommProtocol)


def test_log_transport_always_present_and_first():
    """LogTransport is always first regardless of other transports."""
    comm = _build_comm_protocol(config=None)
    assert isinstance(comm._transports[0], LogTransport)


async def test_failing_extra_transport_does_not_block_log():
    """If an extra transport fails, LogTransport still receives message."""
    bad = MagicMock()
    bad.send = AsyncMock(side_effect=RuntimeError("TUI down"))

    comm = _build_comm_protocol(config=None, extra_transports=[bad])
    await comm.emit_intent(op_id="op-1", goal="g", target_files=[], risk_tier="SAFE_AUTO", blast_radius=1)
    log_transport = comm._transports[0]
    assert len(log_transport.messages) == 1
