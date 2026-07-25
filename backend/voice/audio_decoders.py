"""Adaptive audio decoding — magic-number routing over pluggable strategies.

The vulnerability this removes
------------------------------
The phantom tap reached for ``aifc`` because ``say -o`` emits AIFF. ``aifc`` is
deprecated in Python 3.11 and **deleted in 3.13** (PEP 594), so the visualizer
carried a dated expiry: on the day the host upgrades, Karen's waveform silently
stops. ``audioop``, which the same path used for channel mixing, is removed in
3.13 as well.

Two independent axes, resolved separately
-----------------------------------------
* **Format** comes from the BYTES, never the filename. A container is identified
  by its magic number (``FORM….AIFF``, ``RIFF….WAVE``, ``caff``, ``fLaC``,
  ``OggS``, ``ID3``/frame-sync for MP3). Extensions lie — ``say`` can be told to
  emit WAV into a path still called ``.aiff`` — and a decoder that trusts the
  name fails on exactly that case.
* **Backend** comes from what the HOST can actually do, probed at call time.
  Each strategy declares which formats it handles and whether its dependency is
  importable *now*, so the same code degrades across 3.11 (aifc present) and
  3.13 (aifc gone) with no version checks scattered through the callers.

Strategies, in preference order:

1. ``soundfile`` — libsndfile; handles AIFF/WAV/FLAC/CAF/OGG uniformly. Optional.
2. **native PCM reader** — a self-contained AIFF/WAV parser written here. No
   stdlib audio modules, no third-party dependency, no removal date. This is
   what makes the 3.13 path work with nothing installed.
3. ``wave`` — stdlib, WAV only. NOT deprecated (it survives PEP 594), so it
   stays as a cheap fast path.

Explicitly NOT included: ``aifc`` and ``audioop``. Adding them back would
reintroduce the expiry this module exists to remove.
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
from typing import Callable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: (samples in -1..1 mono, sample_rate)
Decoded = Tuple[List[float], int]

# --------------------------------------------------------------------------
# Format identification — from bytes, never from the filename
# --------------------------------------------------------------------------

FORMAT_AIFF = "aiff"
FORMAT_WAV = "wav"
FORMAT_CAF = "caf"
FORMAT_FLAC = "flac"
FORMAT_OGG = "ogg"
FORMAT_MP3 = "mp3"
FORMAT_UNKNOWN = "unknown"

#: Header bytes needed to identify any supported container.
SNIFF_BYTES = 16


def sniff_format(header: bytes) -> str:
    """Container format from magic numbers. ``FORMAT_UNKNOWN`` when unrecognised.

    AIFF and WAV both use a chunked container whose type lives at offset 8, so
    the leading tag alone is insufficient — ``FORM`` could be AIFF or AIFF-C,
    and ``RIFF`` could be WAVE or AVI. Both bytes ranges are checked."""
    try:
        if not header or len(header) < 4:
            return FORMAT_UNKNOWN
        if header[:4] == b"FORM" and len(header) >= 12:
            if header[8:12] in (b"AIFF", b"AIFC"):
                return FORMAT_AIFF
            return FORMAT_UNKNOWN
        if header[:4] == b"RIFF" and len(header) >= 12:
            if header[8:12] == b"WAVE":
                return FORMAT_WAV
            return FORMAT_UNKNOWN
        if header[:4] == b"caff":
            return FORMAT_CAF
        if header[:4] == b"fLaC":
            return FORMAT_FLAC
        if header[:4] == b"OggS":
            return FORMAT_OGG
        if header[:3] == b"ID3":
            return FORMAT_MP3
        # MPEG frame sync: 11 set bits.
        if len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0:
            return FORMAT_MP3
        return FORMAT_UNKNOWN
    except Exception:  # noqa: BLE001
        return FORMAT_UNKNOWN


def sniff_file(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return sniff_format(fh.read(SNIFF_BYTES))
    except OSError:
        return FORMAT_UNKNOWN


# --------------------------------------------------------------------------
# Native PCM parsing — the dependency-free, expiry-free path
# --------------------------------------------------------------------------


def _ieee80_to_double(raw: bytes) -> float:
    """Decode an 80-bit IEEE-754 extended float — the format AIFF stores its
    sample rate in, and the single reason ``aifc`` was hard to replace by hand.

    Sign(1) | exponent(15, bias 16383) | mantissa(64, EXPLICIT leading bit).
    Unlike float64 there is no implied 1, so the mantissa is used as-is."""
    if len(raw) < 10:
        return 0.0
    sign = -1.0 if raw[0] & 0x80 else 1.0
    exponent = ((raw[0] & 0x7F) << 8) | raw[1]
    mantissa = int.from_bytes(raw[2:10], "big")
    if exponent == 0 and mantissa == 0:
        return 0.0
    if exponent == 0x7FFF:
        return 0.0  # inf/NaN — meaningless as a sample rate
    return sign * mantissa * (2.0 ** (exponent - 16383 - 63))


def _pcm_to_float(raw: bytes, width: int, channels: int, big_endian: bool) -> List[float]:
    """Interleaved integer PCM → mono float in -1..1.

    Channel mixing is done here rather than via ``audioop`` (removed in 3.13):
    the first channel is taken, which is correct for a level meter and avoids
    the cost of averaging on every frame."""
    if width not in (1, 2, 3, 4) or channels < 1:
        return []
    frame = width * channels
    if frame <= 0:
        return []
    usable = len(raw) - (len(raw) % frame)
    out: List[float] = []
    order = "big" if big_endian else "little"
    if width == 1:
        # 8-bit PCM is UNSIGNED in WAV and signed in AIFF; WAV's convention is
        # the common case here and the 128 offset makes silence read as 0.
        for i in range(0, usable, frame):
            out.append((raw[i] - 128) / 128.0)
        return out
    scale = float(1 << (8 * width - 1))
    for i in range(0, usable, frame):
        chunk = raw[i:i + width]
        val = int.from_bytes(chunk, order, signed=True)
        out.append(val / scale)
    return out


def _iter_chunks(data: bytes, start: int, big_endian: bool):
    """Yield ``(chunk_id, payload)`` from a RIFF/IFF chunk stream."""
    fmt = ">I" if big_endian else "<I"
    pos = start
    n = len(data)
    while pos + 8 <= n:
        cid = data[pos:pos + 4]
        (size,) = struct.unpack(fmt, data[pos + 4:pos + 8])
        payload = data[pos + 8:pos + 8 + size]
        yield cid, payload
        pos += 8 + size
        if size % 2:          # IFF/RIFF chunks are word-aligned
            pos += 1


def decode_aiff_native(data: bytes) -> Optional[Decoded]:
    """AIFF/AIFC without ``aifc``. Uncompressed PCM only; a compressed AIFC
    (``COMM`` compression != NONE/sowt) returns None so a real codec backend
    can take it."""
    try:
        if data[:4] != b"FORM" or data[8:12] not in (b"AIFF", b"AIFC"):
            return None
        channels = width = 0
        rate = 0
        little = False
        samples: List[float] = []
        for cid, payload in _iter_chunks(data, 12, big_endian=True):
            if cid == b"COMM" and len(payload) >= 18:
                channels, _frames, bits = struct.unpack(">HIH", payload[:8])
                rate = int(_ieee80_to_double(payload[8:18]))
                width = max(1, (bits + 7) // 8)
                if len(payload) >= 22:
                    comp = payload[18:22]
                    if comp == b"sowt":
                        little = True      # byte-swapped PCM, still uncompressed
                    elif comp not in (b"NONE", b"twos", b"in24", b"in32"):
                        return None        # genuinely compressed
            elif cid == b"SSND" and len(payload) >= 8:
                (offset,) = struct.unpack(">I", payload[0:4])
                samples = _pcm_to_float(
                    payload[8 + offset:], width or 2, channels or 1,
                    big_endian=not little,
                )
        if not rate:
            return None
        return (samples, rate)
    except Exception:  # noqa: BLE001
        return None


def decode_wav_native(data: bytes) -> Optional[Decoded]:
    """WAV without ``wave``/``audioop``. PCM and IEEE-float."""
    try:
        if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            return None
        channels = width = rate = 0
        is_float = False
        samples: List[float] = []
        for cid, payload in _iter_chunks(data, 12, big_endian=False):
            if cid == b"fmt " and len(payload) >= 16:
                tag, channels, rate, _br, _ba, bits = struct.unpack(
                    "<HHIIHH", payload[:16],
                )
                is_float = tag == 3
                width = max(1, bits // 8)
            elif cid == b"data":
                if is_float and width == 4:
                    cnt = len(payload) // 4
                    vals = struct.unpack("<%df" % cnt, payload[:cnt * 4])
                    step = channels or 1
                    samples = [float(v) for v in vals[::step]]
                else:
                    samples = _pcm_to_float(
                        payload, width or 2, channels or 1, big_endian=False,
                    )
        if not rate:
            return None
        return (samples, rate)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------


class DecodeStrategy:
    """One way of turning bytes into samples."""

    name = "abstract"
    formats: Tuple[str, ...] = ()

    def available(self) -> bool:
        raise NotImplementedError

    def decode(self, data: bytes, path: Optional[str] = None) -> Optional[Decoded]:
        raise NotImplementedError

    def handles(self, fmt: str) -> bool:
        return fmt in self.formats


class SoundFileStrategy(DecodeStrategy):
    """libsndfile via ``soundfile``. Broadest coverage; optional dependency."""

    name = "soundfile"
    formats = (FORMAT_AIFF, FORMAT_WAV, FORMAT_FLAC, FORMAT_CAF, FORMAT_OGG)

    def available(self) -> bool:
        try:
            import soundfile  # type: ignore # noqa: F401
            return True
        except Exception:  # noqa: BLE001
            return False

    def decode(self, data: bytes, path: Optional[str] = None) -> Optional[Decoded]:
        try:
            import io

            import soundfile as sf  # type: ignore
            arr, rate = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
            return ([float(row[0]) for row in arr], int(rate))
        except Exception:  # noqa: BLE001
            return None


class NativePCMStrategy(DecodeStrategy):
    """Self-contained AIFF/WAV parser. Always available — no imports beyond
    ``struct``, so it has no deprecation horizon. This is the strategy that
    keeps the visualizer alive on 3.13 with nothing installed."""

    name = "native"
    formats = (FORMAT_AIFF, FORMAT_WAV)

    def available(self) -> bool:
        return True

    def decode(self, data: bytes, path: Optional[str] = None) -> Optional[Decoded]:
        fmt = sniff_format(data[:SNIFF_BYTES])
        if fmt == FORMAT_AIFF:
            return decode_aiff_native(data)
        if fmt == FORMAT_WAV:
            return decode_wav_native(data)
        return None


class StdlibWaveStrategy(DecodeStrategy):
    """stdlib ``wave``. WAV only. Survives PEP 594, so it is safe to keep."""

    name = "wave"
    formats = (FORMAT_WAV,)

    def available(self) -> bool:
        try:
            import wave  # noqa: F401
            return True
        except Exception:  # noqa: BLE001
            return False

    def decode(self, data: bytes, path: Optional[str] = None) -> Optional[Decoded]:
        try:
            import io
            import wave
            with wave.open(io.BytesIO(data), "rb") as fh:
                rate = int(fh.getframerate())
                width = int(fh.getsampwidth())
                ch = int(fh.getnchannels())
                raw = fh.readframes(fh.getnframes())
            return (_pcm_to_float(raw, width, ch, big_endian=False), rate)
        except Exception:  # noqa: BLE001
            return None


#: Preference order. soundfile first (broadest + fastest), native second (always
#: works), stdlib wave last as a cheap WAV fast path.
DEFAULT_STRATEGIES: Tuple[DecodeStrategy, ...] = (
    SoundFileStrategy(), NativePCMStrategy(), StdlibWaveStrategy(),
)


def select_strategies(
    fmt: str, strategies: Optional[Sequence[DecodeStrategy]] = None,
) -> List[DecodeStrategy]:
    """Available strategies that handle ``fmt``, in preference order. Probed at
    call time, so a host that gains or loses ``soundfile`` re-routes with no
    code change and no version checks in the callers."""
    pool = strategies if strategies is not None else DEFAULT_STRATEGIES
    return [s for s in pool if s.handles(fmt) and s.available()]


def decode_bytes(
    data: bytes, *, strategies: Optional[Sequence[DecodeStrategy]] = None,
) -> Optional[Decoded]:
    """Route by magic number, then try each capable strategy until one succeeds.

    Falling through on a None return (not just on an exception) matters: a
    strategy may be importable yet unable to handle a specific sub-format, and
    the next one should still get its turn."""
    fmt = sniff_format(data[:SNIFF_BYTES])
    if fmt == FORMAT_UNKNOWN:
        return None
    for strat in select_strategies(fmt, strategies):
        try:
            out = strat.decode(data)
        except Exception:  # noqa: BLE001
            continue
        if out and out[0] and out[1] > 0:
            return out
    return None


def decode_file_blocking(
    path: str, *, strategies: Optional[Sequence[DecodeStrategy]] = None,
) -> Optional[Decoded]:
    try:
        with open(path, "rb") as fh:
            return decode_bytes(fh.read(), strategies=strategies)
    except OSError:
        return None


async def decode_file(
    path: str, *, strategies: Optional[Sequence[DecodeStrategy]] = None,
) -> Optional[Decoded]:
    """Off-loop decode. File I/O and parsing are blocking, so they run in a
    worker thread — the visualizer's event loop never parses audio."""
    try:
        return await asyncio.to_thread(
            decode_file_blocking, path, strategies=strategies,
        )
    except Exception:  # noqa: BLE001
        return None


def rms_envelope(samples: Sequence[float], sample_rate: int, fps: float) -> List[float]:
    """RMS per frame at ``fps``. Shared by every decode path."""
    if not samples or sample_rate <= 0 or fps <= 0:
        return []
    step = max(1, int(sample_rate / fps))
    out: List[float] = []
    for i in range(0, len(samples), step):
        window = samples[i:i + step]
        if not window:
            break
        total = 0.0
        for s in window:
            total += s * s
        out.append(math.sqrt(total / len(window)))
    return out


__all__ = [
    "DEFAULT_STRATEGIES",
    "FORMAT_AIFF", "FORMAT_CAF", "FORMAT_FLAC", "FORMAT_MP3",
    "FORMAT_OGG", "FORMAT_UNKNOWN", "FORMAT_WAV",
    "DecodeStrategy", "NativePCMStrategy", "SoundFileStrategy",
    "StdlibWaveStrategy",
    "decode_bytes", "decode_file", "decode_file_blocking",
    "rms_envelope", "select_strategies", "sniff_file", "sniff_format",
]
