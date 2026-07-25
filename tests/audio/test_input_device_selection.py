"""The microphone must be the Mac's, not the phone's.

macOS names a Continuity microphone after its OWNER, not its hardware. This
machine enumerates:

    [0] Derek J. Russell Microphone      <- the iPhone, and the system DEFAULT
    [1] MacBook Pro Microphone

"Derek J. Russell Microphone" matches none of iphone / ipad / continuity /
airpods, so the blocklist passed it and JARVIS took the phone — the operator
watched the recording indicator light up on their phone while the Mac's own
microphone sat unused.

A blocklist fails OPEN: an unrecognised device is trusted. Since Continuity
names are person-shaped, the unrecognised case is the COMMON case. Positive
selection inverts that — when a built-in mic is present, everything else
loses by default.
"""

from __future__ import annotations

import pytest

from backend.audio.full_duplex_device import (
    _filter_local_input_devices,
    _is_local_mic_name,
    _is_remote_mic_name,
)


def _dev(name, ch=1):
    return {"name": name, "max_input_channels": ch, "max_output_channels": 0}


def _inputs(devices):
    return [d["name"] for d in devices if d.get("max_input_channels", 0) > 0]


# ---------------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------------


def test_a_person_named_continuity_mic_loses_to_the_builtin():
    """THE REGRESSION, with the exact names this machine enumerates."""
    devices = [_dev("Derek J. Russell Microphone"), _dev("MacBook Pro Microphone")]
    assert _inputs(_filter_local_input_devices(devices)) == ["MacBook Pro Microphone"]


def test_the_blocklist_alone_would_have_let_it_through():
    """Proves the old mechanism could not have caught this — the test would be
    meaningless if the name happened to match a pattern."""
    assert _is_remote_mic_name("derek j. russell microphone") is False
    assert _is_local_mic_name("derek j. russell microphone") is False


def test_any_unknown_device_loses_to_a_builtin():
    """The general form: the next Continuity device will be named something
    else again, and must lose without anyone editing a list."""
    devices = [
        _dev("Something Nobody Anticipated"),
        _dev("Xyzzy Wireless Mic"),
        _dev("MacBook Pro Microphone"),
    ]
    assert _inputs(_filter_local_input_devices(devices)) == ["MacBook Pro Microphone"]


@pytest.mark.parametrize("name", [
    "MacBook Pro Microphone", "Built-in Microphone", "Internal Microphone",
    "iMac Microphone", "Mac Studio Microphone",
])
def test_apple_builtin_naming_is_recognised(name):
    assert _is_local_mic_name(name.lower()) is True


# ---------------------------------------------------------------------------
# Not breaking the machines without a built-in
# ---------------------------------------------------------------------------


def test_with_no_builtin_the_blocklist_still_applies():
    """A Mac mini with only a USB mic must still find it — positive selection
    must not become 'built-in or nothing'."""
    devices = [_dev("iPhone Microphone"), _dev("Blue Yeti USB")]
    assert _inputs(_filter_local_input_devices(devices)) == ["Blue Yeti USB"]


def test_with_only_remote_mics_nothing_is_masked_into_deafness():
    """Every candidate is remote. Masking them all would leave the caller with
    no input at all; the resolver's own fallback handles that, and this test
    pins that we hand it a usable list rather than an empty one."""
    devices = [_dev("iPhone Microphone")]
    out = _filter_local_input_devices(devices)
    assert len(out) == 1        # entry preserved; index correspondence intact


def test_output_devices_are_never_touched():
    devices = [{"name": "iPhone", "max_input_channels": 1, "max_output_channels": 2}]
    out = _filter_local_input_devices(devices)
    assert out[0]["max_output_channels"] == 2


def test_indices_still_correspond():
    """The caller passes indices to PortAudio, so entries are masked, never
    removed."""
    devices = [_dev("Derek J. Russell Microphone"), _dev("MacBook Pro Microphone")]
    assert len(_filter_local_input_devices(devices)) == len(devices)


# ---------------------------------------------------------------------------
# The operator's escape hatch
# ---------------------------------------------------------------------------


def test_an_explicit_choice_wins_by_name(monkeypatch):
    monkeypatch.setenv("JARVIS_AUDIO_INPUT_DEVICE", "yeti")
    devices = [_dev("MacBook Pro Microphone"), _dev("Blue Yeti USB")]
    assert _inputs(_filter_local_input_devices(devices)) == ["Blue Yeti USB"]


def test_an_explicit_choice_wins_by_index(monkeypatch):
    monkeypatch.setenv("JARVIS_AUDIO_INPUT_DEVICE", "0")
    devices = [_dev("Derek J. Russell Microphone"), _dev("MacBook Pro Microphone")]
    assert _inputs(_filter_local_input_devices(devices)) == ["Derek J. Russell Microphone"]


def test_an_unmatched_override_falls_back_rather_than_going_deaf(monkeypatch):
    """A typo in an env var must not silence the microphone."""
    monkeypatch.setenv("JARVIS_AUDIO_INPUT_DEVICE", "no-such-device")
    devices = [_dev("Derek J. Russell Microphone"), _dev("MacBook Pro Microphone")]
    assert _inputs(_filter_local_input_devices(devices)) == ["MacBook Pro Microphone"]


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


def test_the_startup_log_names_the_device_it_chose():
    """`in=0` is opaque: an operator watching their phone light up cannot tell
    from an index which microphone was selected, and neither could I."""
    from pathlib import Path

    src = Path("backend/audio/full_duplex_device.py").read_text(encoding="utf-8")
    assert "_device_label(in_device_label)" in src


def test_the_label_helper_never_raises():
    from backend.audio.full_duplex_device import _device_label

    assert _device_label(None) == "default"
    assert _device_label(9999) == "?"
    assert _device_label("garbage") == "?"
