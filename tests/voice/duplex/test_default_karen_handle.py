"""Default-Karen singleton -- the same late-binding pattern as
voice_command_sensor.set_default_voice_sensor (Sprint 4)."""
from __future__ import annotations

from backend.core.ouroboros.governance.comms.duplex import karen_duplex_factory as kdf


def test_default_handle_roundtrip():
    sentinel = object()
    kdf.set_default_karen(sentinel)
    try:
        assert kdf.get_default_karen() is sentinel
    finally:
        kdf.set_default_karen(None)
    assert kdf.get_default_karen() is None
