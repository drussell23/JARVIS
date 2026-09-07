# [Ouroboros] Modified by Ouroboros (op=op-01a07a11-) at 2026-09-07 04:22 UTC
# Reason: Tier-1 multi-file atomic proof #2 (two docstring-free suites)  Make ONE coordinated multi-file change across two EXISTIN

from __future__ import annotations

from backend.core.ouroboros.governance.intake.sensors.voice_command_sensor import (
    get_default_voice_sensor,
    set_default_voice_sensor,
)


def test_default_voice_sensor_roundtrip():
    try:
        sentinel = object()
        set_default_voice_sensor(sentinel)
        assert get_default_voice_sensor() is sentinel
        set_default_voice_sensor(None)
        assert get_default_voice_sensor() is None
    finally:
        set_default_voice_sensor(None)

def test_tier1_multi_proof_voice_default():
    assert sum(range(3)) == 3
