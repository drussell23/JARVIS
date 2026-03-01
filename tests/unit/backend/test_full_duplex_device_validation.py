"""Validation tests for FullDuplexDevice capability negotiation."""

import types

import pytest

import backend.audio.full_duplex_device as fd_mod
from backend.audio.full_duplex_device import DeviceConfig, FullDuplexDevice


@pytest.fixture(autouse=True)
def _clear_audio_env(monkeypatch):
    """Ensure tests run with deterministic audio env defaults."""
    for key in (
        "JARVIS_AUDIO_INPUT_DEVICE",
        "JARVIS_AUDIO_OUTPUT_DEVICE",
        "JARVIS_AUDIO_REQUIRE_INPUT",
        "JARVIS_AUDIO_ALLOW_OUTPUT_ONLY",
    ):
        monkeypatch.delenv(key, raising=False)


def _make_fake_sd(
    *,
    default_device=(1, 2),
    devices=None,
    invalid_input_devices=None,
    invalid_output_devices=None,
):
    """Create a fake sounddevice surface for device-selection tests."""
    devices = devices or [
        {"name": "duplex0", "max_input_channels": 1, "max_output_channels": 1},
        {"name": "duplex1", "max_input_channels": 1, "max_output_channels": 1},
        {"name": "duplex2", "max_input_channels": 1, "max_output_channels": 1},
    ]
    invalid_input_devices = set(invalid_input_devices or [])
    invalid_output_devices = set(invalid_output_devices or [])

    fake = types.SimpleNamespace()
    fake.default = types.SimpleNamespace(device=default_device)

    def _query_devices(index=None):
        if index is None:
            return devices
        return devices[index]

    def _check_input_settings(**kwargs):
        dev = int(kwargs["device"])
        if dev in invalid_input_devices:
            raise ValueError(f"input device {dev} invalid")
        caps = int(devices[dev].get("max_input_channels", 0))
        if caps < int(kwargs.get("channels", 1)):
            raise ValueError(f"input device {dev} has insufficient channels")

    def _check_output_settings(**kwargs):
        dev = int(kwargs["device"])
        if dev in invalid_output_devices:
            raise ValueError(f"output device {dev} invalid")
        caps = int(devices[dev].get("max_output_channels", 0))
        if caps < int(kwargs.get("channels", 1)):
            raise ValueError(f"output device {dev} has insufficient channels")

    class _DummyStream:
        def __init__(self, **_kwargs):
            self.started = False

        def start(self):
            self.started = True

        def stop(self):
            self.started = False

        def close(self):
            self.started = False

    class _DummyOutputStream(_DummyStream):
        pass

    fake.query_devices = _query_devices
    fake.check_input_settings = _check_input_settings
    fake.check_output_settings = _check_output_settings
    fake.Stream = _DummyStream
    fake.OutputStream = _DummyOutputStream
    return fake


def test_validate_device_selection_uses_defaults(monkeypatch):
    """Validation should resolve and persist default duplex devices."""
    monkeypatch.delenv("JARVIS_AUDIO_INPUT_DEVICE", raising=False)
    monkeypatch.delenv("JARVIS_AUDIO_OUTPUT_DEVICE", raising=False)
    monkeypatch.setattr(fd_mod, "sd", _make_fake_sd(default_device=(1, 2)))

    device = FullDuplexDevice(DeviceConfig())
    device._validate_device_selection()

    assert device.config.input_device == 1
    assert device.config.output_device == 2
    assert device.input_enabled is True


def test_validate_device_selection_falls_back_to_output_only(monkeypatch):
    """No input-capable device should degrade to output-only when allowed."""
    output_only_devices = [
        {"name": "spk0", "max_input_channels": 0, "max_output_channels": 2},
        {"name": "spk1", "max_input_channels": 0, "max_output_channels": 2},
    ]
    monkeypatch.setattr(
        fd_mod,
        "sd",
        _make_fake_sd(default_device=(-1, 0), devices=output_only_devices),
    )

    cfg = DeviceConfig(require_input=False, allow_output_only=True)
    device = FullDuplexDevice(cfg)
    device._validate_device_selection()

    assert device.config.output_device == 0
    assert device.config.input_device is None
    assert device.input_enabled is False


def test_validate_device_selection_requires_input_when_configured(monkeypatch):
    """If input is required, output-only fallback is rejected."""
    output_only_devices = [
        {"name": "spk0", "max_input_channels": 0, "max_output_channels": 2},
        {"name": "spk1", "max_input_channels": 0, "max_output_channels": 2},
    ]
    monkeypatch.setattr(
        fd_mod,
        "sd",
        _make_fake_sd(default_device=(-1, 0), devices=output_only_devices),
    )

    cfg = DeviceConfig(require_input=True, allow_output_only=False)
    device = FullDuplexDevice(cfg)
    with pytest.raises(RuntimeError, match="No valid input device available"):
        device._validate_device_selection()


@pytest.mark.asyncio
async def test_start_uses_output_stream_when_input_unavailable(monkeypatch):
    """start() should open OutputStream in negotiated output-only mode."""
    output_only_devices = [
        {"name": "spk0", "max_input_channels": 0, "max_output_channels": 2},
    ]
    fake_sd = _make_fake_sd(default_device=(-1, 0), devices=output_only_devices)

    called = {"stream": 0, "output_stream": 0}

    class _GuardedStream:
        def __init__(self, **_kwargs):
            called["stream"] += 1
            raise AssertionError("Duplex Stream should not be used in output-only mode")

    class _TrackOutputStream:
        def __init__(self, **_kwargs):
            called["output_stream"] += 1
            self.started = False

        def start(self):
            self.started = True

        def stop(self):
            self.started = False

        def close(self):
            self.started = False

    fake_sd.Stream = _GuardedStream
    fake_sd.OutputStream = _TrackOutputStream
    monkeypatch.setattr(fd_mod, "sd", fake_sd)

    cfg = DeviceConfig(require_input=False, allow_output_only=True)
    device = FullDuplexDevice(cfg)
    await device.start()

    assert device.input_enabled is False
    assert device.is_running is True
    assert called["stream"] == 0
    assert called["output_stream"] == 1

    await device.stop()


@pytest.mark.asyncio
async def test_start_and_stop_use_dedicated_audio_executor(monkeypatch):
    """Audio I/O should not compete with the generic executor during startup."""
    fake_sd = _make_fake_sd()
    monkeypatch.setattr(fd_mod, "sd", fake_sd)

    captured_executors = []

    class _FakeLoop:
        async def run_in_executor(self, executor, func):
            captured_executors.append(executor)
            return func()

    monkeypatch.setattr(fd_mod.asyncio, "get_running_loop", lambda: _FakeLoop())

    device = FullDuplexDevice(DeviceConfig())
    await device.start()
    await device.stop()

    assert captured_executors == [
        fd_mod._AUDIO_IO_EXECUTOR,
        fd_mod._AUDIO_IO_EXECUTOR,
    ]


def test_validate_device_selection_fails_when_no_output_device(monkeypatch):
    """Output device is mandatory; startup must fail if none is valid."""
    no_output_devices = [
        {"name": "mic0", "max_input_channels": 1, "max_output_channels": 0},
        {"name": "mic1", "max_input_channels": 1, "max_output_channels": 0},
    ]
    monkeypatch.setattr(
        fd_mod,
        "sd",
        _make_fake_sd(default_device=(0, -1), devices=no_output_devices),
    )

    device = FullDuplexDevice(DeviceConfig())
    with pytest.raises(RuntimeError, match="No valid output device available"):
        device._validate_device_selection()
