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
