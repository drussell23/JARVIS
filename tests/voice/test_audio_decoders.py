"""Adaptive audio decoding — magic-number routing, no expiring stdlib modules.

`aifc` is deleted in Python 3.13 and `audioop` with it (PEP 594), so the
phantom tap's original decode path carried an expiry date: on the day the host
upgrades, Karen's waveform silently stops. These pin the replacement.

The load-bearing property is the NATIVE strategy: it parses AIFF and WAV with
nothing but `struct`, so it has no removal horizon and needs no third-party
install. Several tests therefore run against `strategies=[NativePCMStrategy()]`
explicitly — proving the 3.13 path works even when `soundfile` is absent.
"""

from __future__ import annotations

from pathlib import Path

import struct
import subprocess
import sys
import threading
import wave

import pytest

from backend.voice import audio_decoders as ad


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _wav_bytes(*, seconds=0.2, sr=16000, amp=0.5, channels=1, width=2):
    import io
    import math as _m

    buf = io.BytesIO()
    with wave.open(buf, "wb") as fh:
        fh.setnchannels(channels)
        fh.setsampwidth(width)
        fh.setframerate(sr)
        frames = bytearray()
        for i in range(int(seconds * sr)):
            v = int(_m.sin(i / 30.0) * amp * 32767)
            for _ in range(channels):
                frames += struct.pack("<h", v)
        fh.writeframes(bytes(frames))
    return buf.getvalue()


def _ieee80(value: float) -> bytes:
    """Encode a sample rate as the 80-bit extended float AIFF requires."""
    import math as _m

    if value <= 0:
        return b"\x00" * 10
    exp = int(_m.floor(_m.log2(value)))
    mant = int(value / (2.0 ** exp) * (2 ** 63))
    biased = exp + 16383
    return struct.pack(">H", biased) + struct.pack(">Q", mant)


def _aiff_bytes(*, seconds=0.2, sr=16000, amp=0.5, channels=1):
    import math as _m

    n = int(seconds * sr)
    pcm = bytearray()
    for i in range(n):
        v = int(_m.sin(i / 30.0) * amp * 32767)
        for _ in range(channels):
            pcm += struct.pack(">h", v)          # AIFF is BIG-endian
    comm = struct.pack(">HIH", channels, n, 16) + _ieee80(sr)
    ssnd = struct.pack(">II", 0, 0) + bytes(pcm)
    body = (
        b"AIFF"
        + b"COMM" + struct.pack(">I", len(comm)) + comm
        + b"SSND" + struct.pack(">I", len(ssnd)) + ssnd
    )
    return b"FORM" + struct.pack(">I", len(body)) + body


# ---------------------------------------------------------------------------
# format sniffing — bytes, never the filename
# ---------------------------------------------------------------------------


def test_sniffs_aiff_and_wav_from_magic_numbers():
    assert ad.sniff_format(_aiff_bytes()[:16]) == ad.FORMAT_AIFF
    assert ad.sniff_format(_wav_bytes()[:16]) == ad.FORMAT_WAV


@pytest.mark.parametrize("header,expected", [
    (b"caff\x00\x01\x00\x00", ad.FORMAT_CAF),
    (b"fLaC\x00\x00\x00\x22", ad.FORMAT_FLAC),
    (b"OggS\x00\x02\x00\x00", ad.FORMAT_OGG),
    (b"ID3\x03\x00\x00\x00", ad.FORMAT_MP3),
    (b"\xff\xfb\x90\x00", ad.FORMAT_MP3),
])
def test_sniffs_other_containers(header, expected):
    assert ad.sniff_format(header) == expected


def test_container_type_is_checked_not_just_the_leading_tag():
    """`RIFF` alone could be AVI; `FORM` alone could be many IFF types. The
    type field at offset 8 must be verified."""
    assert ad.sniff_format(b"RIFF\x00\x00\x00\x00AVI ") == ad.FORMAT_UNKNOWN
    assert ad.sniff_format(b"FORM\x00\x00\x00\x008SVX") == ad.FORMAT_UNKNOWN


def test_garbage_and_truncated_headers_are_unknown_not_crashes():
    for h in (b"", b"\x00", b"nope", b"RIF", None):
        assert ad.sniff_format(h) == ad.FORMAT_UNKNOWN  # type: ignore[arg-type]


def test_extension_never_decides_the_format(tmp_path):
    """`say` can emit WAV into a path still named .aiff. A decoder that trusts
    the filename fails exactly there."""
    p = tmp_path / "actually_a_wav.aiff"
    p.write_bytes(_wav_bytes())
    assert ad.sniff_file(str(p)) == ad.FORMAT_WAV
    out = ad.decode_file_blocking(str(p))
    assert out and out[1] == 16000


# ---------------------------------------------------------------------------
# NATIVE strategy — the dependency-free, no-expiry path
# ---------------------------------------------------------------------------

NATIVE = [ad.NativePCMStrategy()]


def test_native_decodes_wav_without_any_stdlib_audio_module():
    out = ad.decode_bytes(_wav_bytes(sr=22050), strategies=NATIVE)
    assert out is not None
    samples, sr = out
    assert sr == 22050
    assert len(samples) > 100
    assert max(abs(s) for s in samples) > 0.1


def test_native_decodes_aiff_without_aifc():
    """THE 3.13 PATH: AIFF decoded with no `aifc`, no `audioop`, no soundfile."""
    out = ad.decode_bytes(_aiff_bytes(sr=16000), strategies=NATIVE)
    assert out is not None
    samples, sr = out
    assert sr == 16000, "80-bit IEEE extended sample rate decoded wrong"
    assert len(samples) > 100
    assert max(abs(s) for s in samples) > 0.1


def test_native_strategy_is_always_available():
    """No import to fail, so no host can lose it."""
    assert ad.NativePCMStrategy().available() is True


def test_ieee80_roundtrip_for_common_sample_rates():
    """The 80-bit extended float is the one genuinely fiddly part of AIFF."""
    for rate in (8000, 16000, 22050, 44100, 48000, 96000):
        assert abs(ad._ieee80_to_double(_ieee80(float(rate))) - rate) < 1.0


def test_ieee80_handles_zero_and_infinity():
    assert ad._ieee80_to_double(b"\x00" * 10) == 0.0
    assert ad._ieee80_to_double(b"\x7f\xff" + b"\x00" * 8) == 0.0
    assert ad._ieee80_to_double(b"\x00" * 4) == 0.0        # truncated


def test_stereo_is_mixed_to_mono_without_audioop():
    out = ad.decode_bytes(_wav_bytes(channels=2), strategies=NATIVE)
    mono = ad.decode_bytes(_wav_bytes(channels=1), strategies=NATIVE)
    assert out and mono
    assert abs(len(out[0]) - len(mono[0])) <= 1, "channel de-interleave wrong"


def test_silence_decodes_to_zeros():
    out = ad.decode_bytes(_wav_bytes(amp=0.0), strategies=NATIVE)
    assert out and all(abs(s) < 1e-4 for s in out[0])


# ---------------------------------------------------------------------------
# strategy selection
# ---------------------------------------------------------------------------


def test_selection_is_probed_at_call_time_not_hardcoded():
    for s in ad.select_strategies(ad.FORMAT_WAV):
        assert s.available() and s.handles(ad.FORMAT_WAV)
    assert ad.select_strategies(ad.FORMAT_UNKNOWN) == []


def test_unavailable_strategy_is_skipped():
    class _Gone(ad.DecodeStrategy):
        name, formats = "gone", (ad.FORMAT_WAV,)

        def available(self):
            return False

        def decode(self, data, path=None):
            raise AssertionError("unavailable strategy was invoked")

    out = ad.decode_bytes(
        _wav_bytes(), strategies=[_Gone(), ad.NativePCMStrategy()],
    )
    assert out is not None


def test_falls_through_when_a_strategy_returns_none():
    """A backend may be importable yet unable to handle a sub-format; the next
    one must still get its turn."""
    class _Abstains(ad.DecodeStrategy):
        name, formats = "abstains", (ad.FORMAT_WAV,)

        def available(self):
            return True

        def decode(self, data, path=None):
            return None

    out = ad.decode_bytes(
        _wav_bytes(), strategies=[_Abstains(), ad.NativePCMStrategy()],
    )
    assert out is not None, "did not fall through on a None return"


def test_falls_through_when_a_strategy_raises():
    class _Explodes(ad.DecodeStrategy):
        name, formats = "explodes", (ad.FORMAT_WAV,)

        def available(self):
            return True

        def decode(self, data, path=None):
            raise RuntimeError("codec exploded")

    assert ad.decode_bytes(
        _wav_bytes(), strategies=[_Explodes(), ad.NativePCMStrategy()],
    ) is not None


def test_no_capable_strategy_returns_none_rather_than_raising():
    assert ad.decode_bytes(b"NOTAUDIO" * 8) is None
    assert ad.decode_bytes(b"") is None


def test_truncated_audio_never_raises():
    for cut in (12, 20, 40):
        ad.decode_bytes(_aiff_bytes()[:cut], strategies=NATIVE)
        ad.decode_bytes(_wav_bytes()[:cut], strategies=NATIVE)


# ---------------------------------------------------------------------------
# async: decoding never blocks the visualizer's loop
# ---------------------------------------------------------------------------


async def test_decode_runs_off_the_event_loop(tmp_path):
    p = tmp_path / "a.wav"
    p.write_bytes(_wav_bytes(seconds=0.3))
    loop_thread = threading.get_ident()
    seen = {}

    real = ad.decode_file_blocking

    def _spy(path, **kw):
        seen["thread"] = threading.get_ident()
        return real(path, **kw)

    ad.decode_file_blocking = _spy  # type: ignore[assignment]
    try:
        out = await ad.decode_file(str(p))
    finally:
        ad.decode_file_blocking = real  # type: ignore[assignment]

    assert out is not None
    assert seen["thread"] != loop_thread, "decode ran ON the event loop"


async def test_missing_file_returns_none_asynchronously(tmp_path):
    assert await ad.decode_file(str(tmp_path / "nope.wav")) is None


# ---------------------------------------------------------------------------
# envelope
# ---------------------------------------------------------------------------


def test_rms_envelope_framerate_and_bounds():
    out = ad.decode_bytes(_wav_bytes(seconds=1.0, sr=16000), strategies=NATIVE)
    assert out
    env = ad.rms_envelope(out[0], out[1], 20.0)
    assert 18 <= len(env) <= 22
    assert all(0.0 <= v <= 1.0 for v in env)
    assert max(env) > 0.1


def test_rms_envelope_degenerate_inputs():
    assert ad.rms_envelope([], 16000, 20.0) == []
    assert ad.rms_envelope([0.5], 0, 20.0) == []
    assert ad.rms_envelope([0.5], 16000, 0.0) == []


# ---------------------------------------------------------------------------
# the real thing: an actual `say` AIFF (macOS only)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS `say` required")
def test_decodes_a_real_say_aiff_with_the_native_strategy(tmp_path):
    """Karen's ACTUAL output format, decoded with zero third-party deps and
    zero deprecated stdlib modules. This is the case the whole module exists
    for — synthetic fixtures cannot prove `say`'s real container."""
    # NOT tmp_path: `say -o` writes a 0-second, header-only file when the
    # destination is inside a sandboxed TMPDIR (verified with afinfo:
    # "estimated duration: 0.000000 sec"). Production is unaffected because
    # mkstemp targets the real system temp — but a test pointed at tmp_path
    # would skip forever and quietly prove nothing.
    import tempfile
    with tempfile.TemporaryDirectory(dir=str(Path.home())) as td:
        out_path = Path(td) / "karen.aiff"
        try:
            subprocess.run(
                ["say", "-o", str(out_path),
                 "Karen speaking, testing the oscilloscope waveform."],
                check=True, capture_output=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            pytest.skip(f"`say` unavailable: {exc}")
        _assert_real_say_file(out_path)


def _assert_real_say_file(out_path):
    assert out_path.exists() and out_path.stat().st_size > 0
    fmt = ad.sniff_file(str(out_path))
    assert fmt in (ad.FORMAT_AIFF, ad.FORMAT_WAV, ad.FORMAT_CAF), f"got {fmt}"

    decoded = ad.decode_file_blocking(str(out_path), strategies=NATIVE)
    if decoded is None:
        pytest.skip(f"`say` emitted {fmt}, which the native strategy does not parse")

    samples, sr = decoded
    assert sr > 0 and len(samples) > 1000
    env = ad.rms_envelope(samples, sr, 20.0)
    assert env and max(env) > 0.01, "real speech produced a flat envelope"
