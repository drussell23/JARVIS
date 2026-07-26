#!/usr/bin/env python3
"""Golden Master injector — decide whether the STT pipeline can read speech.

Why this exists
---------------
Every remaining theory about the empty-transcript fault is a claim about one
of two layers, and live voice cannot separate them: a spoken test carries the
microphone, the room, macOS Voice Isolation, proximity and the operator's
delivery all at once, so a failure indicts everything and proves nothing.

This drives the SAME ``StreamingSTTEngine`` the plane runs — same VAD, same
endpointer, same pre-roll, same partial cadence, same faster-whisper settings
— from audio whose content is known in advance. Nothing here reimplements the
pipeline; that would test a copy of it. The engine is fed through its real
entry point, ``on_audio_frame``, in real time at the exact 20 ms / 16 kHz
cadence ``AudioBus`` delivers, so the timing-dependent logic behaves as it
does in production.

  golden PASSES, live FAILS  -> software is sound; the fault is hardware, the
                                room, or OS-level capture processing.
  golden FAILS               -> the fault is in the pipeline, and it is now
                                reproducible on demand without speaking.

Modes
-----
  --golden ["phrase"]   synthesize a phrase with macOS ``say`` and run it
  --replay <wav>        run any wav (16 kHz mono is used as-is, else resampled)
  --incident <dir>      run all three tracks of a capture-forensics incident:
                        raw_device / processed_bus / model_input, which
                        answers "is the audio we actually captured readable?"

Not part of the plane. Nothing here is imported by production code, and it
takes no lease and touches no microphone — it can run WHILE the plane is live
without contending for CoreAudio, which is what makes a simultaneous
dual-track possible.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

RATE = 16000
FRAME_MS = 20
FRAME = RATE * FRAME_MS // 1000

DEFAULT_PHRASE = (
    "Hello Karen, this is the golden master test of the transcription pipeline."
)


# ---------------------------------------------------------------------------
# audio sources
# ---------------------------------------------------------------------------

def _read_wav(path: Path) -> Tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        width = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"{path.name}: expected 16-bit PCM, got {width * 8}-bit")
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        a = a.reshape(-1, channels).mean(axis=1)
    return a, rate


def _resample(a: np.ndarray, src: int, dst: int) -> np.ndarray:
    """Linear resample. Adequate here: the question is whether the pipeline
    can read speech, not whether this script is a mastering-grade SRC."""
    if src == dst or a.size == 0:
        return a
    n = int(round(a.size * dst / src))
    return np.interp(
        np.linspace(0.0, a.size - 1, n, dtype=np.float64),
        np.arange(a.size, dtype=np.float64),
        a.astype(np.float64),
    ).astype(np.float32)


def synthesize(phrase: str, voice: Optional[str] = None) -> np.ndarray:
    """A Golden Master built from real speech.

    Deliberately NOT a synthetic waveform. Whisper is an acoustic model of
    human speech; a mathematically constructed tone or noise-modulated
    envelope is not language and would return empty for entirely legitimate
    reasons, proving nothing about the pipeline. ``say`` produces genuine
    speech with a transcript known exactly in advance."""
    with tempfile.TemporaryDirectory() as td:
        aiff = Path(td) / "gm.aiff"
        cmd = ["say", "-o", str(aiff)]
        if voice:
            cmd += ["-v", voice]
        cmd += [phrase]
        subprocess.run(cmd, check=True, capture_output=True)

        wav = Path(td) / "gm.wav"
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", f"LEI16@{RATE}", "-c", "1",
             str(aiff), str(wav)],
            check=True, capture_output=True,
        )
        a, rate = _read_wav(wav)
    return _resample(a, rate, RATE)


# ---------------------------------------------------------------------------
# the harness
# ---------------------------------------------------------------------------

def _lead_out(a: np.ndarray, silence_ms: int = 1200) -> np.ndarray:
    """Real capture never ends the instant speech does, and the endpointer
    needs sustained silence to fire a FINAL. Without a tail the clip ends
    mid-utterance and only partials are ever emitted."""
    pad = np.zeros(int(RATE * silence_ms / 1000), dtype=np.float32)
    # A touch of room tone rather than digital zero: webrtcvad treats an
    # absolutely silent frame differently from a quiet one.
    pad += (1e-4 * np.random.default_rng(0).standard_normal(pad.size)).astype(
        np.float32,
    )
    return np.concatenate([pad[: RATE // 5], a, pad])


async def run_track(
    label: str, audio: np.ndarray, *, realtime: bool = True,
    settle_s: float = 12.0,
) -> List[Tuple[bool, str]]:
    """Feed one track through a private engine instance and collect events.

    A PRIVATE engine per track is the point. Mixing a reference signal into
    the live bus would sum two voices into one buffer and Whisper would decode
    the overlap — an empty result would then indict neither track. Same code,
    same configuration, separate instances: that is what makes the tracks
    independent rather than merely concurrent."""
    from backend.voice.streaming_stt import StreamingSTTEngine

    engine = StreamingSTTEngine(sample_rate=RATE)
    print(f"[{label}] loading model ({os.getenv('JARVIS_STT_MODEL', 'base')})…",
          flush=True)
    t0 = time.monotonic()
    await engine.start()
    print(f"[{label}] model ready in {time.monotonic() - t0:.1f}s "
          f"({audio.size / RATE:.2f}s of audio to feed)", flush=True)

    events: List[Tuple[bool, str]] = []

    async def collect() -> None:
        async for ev in engine.get_transcripts():
            events.append((ev.is_partial, ev.text))
            kind = "partial" if ev.is_partial else "FINAL  "
            print(f"[{label}] {kind} | {ev.text!r}", flush=True)

    collector = asyncio.create_task(collect())

    async def feed() -> None:
        # on_audio_frame is called from AudioBus's capture thread in
        # production. A thread here keeps that shape, and keeps the feed off
        # the loop that has to service the transcription tasks.
        def _pump() -> None:
            for start in range(0, audio.size - FRAME + 1, FRAME):
                engine.on_audio_frame(audio[start:start + FRAME].copy())
                if realtime:
                    time.sleep(FRAME_MS / 1000.0)
        await asyncio.to_thread(_pump)

    await feed()
    # The endpoint FINAL is scheduled from the last frames and decoded in an
    # executor; give it room to land rather than tearing the engine down.
    deadline = time.monotonic() + settle_s
    while time.monotonic() < deadline:
        if any(not p for p, _ in events):
            break
        await asyncio.sleep(0.25)

    await engine.stop()
    collector.cancel()
    try:
        await collector
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    return events


#: What faster-whisper emits when handed audio that is NOT speech. These are
#: not transcripts, they are the model's prior leaking through — and treating
#: them as success is worse than reporting nothing, because it converts "the
#: mic heard noise" into "the pipeline works". `capture_forensics` documents
#: the canonical one: thresholds disabled, non-speech renders as
#: "I'm sorry, I'm sorry, I'm sorry".
_HALLUCINATIONS = {
    "i'm sorry", "im sorry", "sorry", "thank you", "thanks", "you",
    "thanks for watching", "thank you for watching", "bye", "okay", "ok",
    "please subscribe", "subscribe", "the", "yeah", "mm", "hmm", "uh",
}


def _is_hallucination(text: str) -> bool:
    """True when the text is only the model's non-speech prior.

    Compared on the whole utterance, not per word: a real sentence containing
    "thank you" is speech, whereas an utterance that IS "Thank you." from a
    0.9s buffer of room tone is the prior."""
    t = " ".join(text.lower().replace(",", " ").split()).strip(" .!?")
    if not t:
        return True
    if t in _HALLUCINATIONS:
        return True
    # "I'm sorry. I'm sorry. I'm sorry." — the same fragment repeated.
    parts = [p.strip() for p in t.replace("!", ".").split(".") if p.strip()]
    return bool(parts) and all(p in _HALLUCINATIONS for p in parts)


def verdict(label: str, events: List[Tuple[bool, str]], expected: Optional[str]) -> bool:
    finals = [t for p, t in events if not p and t.strip()]
    partials = [t for p, t in events if p and t.strip()]
    print(f"\n[{label}] {len(finals)} final(s), {len(partials)} non-empty partial(s)")
    if not finals and not partials:
        print(f"[{label}] RESULT: FAIL — the pipeline produced no text at all")
        return False
    heard = " ".join(finals) if finals else " ".join(partials)
    print(f"[{label}] heard: {heard!r}")
    if _is_hallucination(heard):
        print(f"[{label}] RESULT: FAIL — {heard!r} is whisper's non-speech "
              f"prior, not a transcript. The audio is not intelligible speech.")
        return False
    if expected:
        want = {w.strip(".,!?").lower() for w in expected.split() if len(w) > 3}
        got = {w.strip(".,!?").lower() for w in heard.split()}
        hit = len(want & got) / max(1, len(want))
        print(f"[{label}] content match: {hit:.0%} of expected keywords")
        ok = hit >= 0.5
        print(f"[{label}] RESULT: {'PASS' if ok else 'FAIL'}")
        return ok
    print(f"[{label}] RESULT: PASS — transcribed")
    return True


# ---------------------------------------------------------------------------

async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--golden", nargs="?", const=DEFAULT_PHRASE, metavar="PHRASE",
                     help="synthesize a phrase with `say` and run it")
    src.add_argument("--replay", metavar="WAV", help="run an existing wav")
    src.add_argument("--incident", metavar="DIR",
                     help="run all tracks of a capture-forensics incident")
    ap.add_argument("--voice", default=None, help="`say` voice (default system)")
    ap.add_argument("--fast", action="store_true",
                    help="feed faster than real time (skews VAD timing)")
    args = ap.parse_args()

    # This is a diagnostic run outside the supervisor's startup sequence, so
    # the ASR admission barrier has no startup to protect here.
    os.environ.setdefault("JARVIS_ASR_ADMISSION_FORCE_OPEN", "1")
    # The engine's own forensics would otherwise write incidents for these
    # synthetic runs and evict real evidence from the 8-slot ring.
    os.environ.setdefault("JARVIS_CAPTURE_FORENSICS", "0")

    realtime = not args.fast
    ok = True

    if args.golden:
        print(f"Golden Master phrase: {args.golden!r}")
        audio = _lead_out(synthesize(args.golden, args.voice))
        ev = await run_track("golden", audio, realtime=realtime)
        ok = verdict("golden", ev, args.golden)

    elif args.replay:
        a, rate = _read_wav(Path(args.replay))
        audio = _lead_out(_resample(a, rate, RATE))
        ev = await run_track(Path(args.replay).stem, audio, realtime=realtime)
        ok = verdict(Path(args.replay).stem, ev, None)

    else:
        root = Path(args.incident)
        for name in ("model_input", "processed_bus", "raw_device"):
            p = root / f"{name}.wav"
            if not p.exists():
                print(f"[{name}] absent — skipped")
                continue
            a, rate = _read_wav(p)
            print(f"\n=== {name}: {a.size / rate:.2f}s @ {rate}Hz "
                  f"peak={np.max(np.abs(a)):.4f} ===")
            audio = _lead_out(_resample(a, rate, RATE))
            ev = await run_track(name, audio, realtime=realtime)
            verdict(name, ev, None)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
