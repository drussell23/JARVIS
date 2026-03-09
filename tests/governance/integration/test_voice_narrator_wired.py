"""VoiceNarrator import path resolves correctly after fix."""
from backend.core.ouroboros.governance.integration import _build_comm_protocol


async def test_build_comm_protocol_includes_voice_narrator():
    """After import fix, VoiceNarrator appears in CommProtocol transports."""
    protocol = _build_comm_protocol()
    transport_types = [type(t).__name__ for t in protocol._transports]
    assert "VoiceNarrator" in transport_types, (
        f"VoiceNarrator missing from transports: {transport_types}. "
        "Fix: backend/core/ouroboros/governance/integration.py safe_say import"
    )
