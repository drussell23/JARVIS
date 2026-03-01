"""AudioBus behavior tests for output-only device mode."""

import types

import pytest

import backend.audio.audio_bus as audio_bus_mod
from backend.audio.audio_bus import AudioBus, DeviceConfig


@pytest.mark.asyncio
async def test_audio_bus_starts_in_output_only_mode_without_mic_registration(monkeypatch):
    """AudioBus should not register mic callback when device has no input."""

    class _FakeDevice:
        def __init__(self, config):
            self.config = config
            self.input_enabled = False
            self.is_running = False
            self.playback_buffer = types.SimpleNamespace(available=0)
            self._add_capture_calls = 0

        @property
        def sample_rate(self):
            return self.config.sample_rate

        async def start(self, progress_callback=None, profile_strategy="balanced"):
            self.is_running = True

        async def stop(self):
            self.is_running = False

        def request_cancel(self):
            pass

        def add_capture_callback(self, _cb):
            self._add_capture_calls += 1

        def remove_capture_callback(self, _cb):
            pass

        def write_playback(self, _audio):
            return 0

        def flush_playback(self):
            return 0

        def get_last_output_frame(self):
            return []

    monkeypatch.setattr(audio_bus_mod, "FullDuplexDevice", _FakeDevice)

    # Clear singleton state so test doesn't interfere with others
    old_instance = AudioBus._instance
    AudioBus._instance = None

    try:
        bus = AudioBus()
        await bus.start(DeviceConfig())
        status = bus.get_status()

        assert status["running"] is True
        assert status["input_enabled"] is False
        assert bus.device is not None
        assert bus.device._add_capture_calls == 0

        await bus.stop()
    finally:
        AudioBus._instance = old_instance
