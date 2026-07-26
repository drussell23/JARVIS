#!/usr/bin/env python3
"""Hunt the hardware ghost: why does this microphone hear a speaker but not a person?

The observation
---------------
A sentence played through the laptop speakers transcribes perfectly. The
operator speaking at a normal distance produces audio whose raw device tap —
the earliest point in the chain, before AEC, AGC, resampling or anything else
this codebase does — contains no speech Whisper can read. Measured::

    operator's voice   peak 0.070   rms 0.0029   crest 27.6dB
    played probe       peak 0.901   rms 0.0793   crest 21.1dB

27x quieter, and 6dB more peaky: sparse transients over a very quiet floor.
That is not a voice; that is near-silence with spikes in it.

The hypothesis under test
-------------------------
The M1 lid holds a three-microphone beamforming array. If CoreAudio is
directional-nulling or phase-inverting elements and then downmixing to mono,
vocal frequencies arriving from the null direction would sum toward zero while
a source in a different direction (the speakers, inches away and off-axis)
survives. That would explain every measurement above.

What this script found on THIS machine, and why it still runs
-------------------------------------------------------------
``max_input_channels = 1``. Channel counts 2, 3 and 4 are refused with
``PortAudioError``. The array is downmixed BELOW the layer PortAudio can
address, so the individual elements cannot be captured here and the
phase-cancellation hypothesis cannot be confirmed or refuted by splitting
channels — on this device.

The script still runs, because "the channels are unreachable" is itself a
finding worth proving reproducibly rather than asserting, and because the
remaining measurements narrow the fault regardless:

  * every input device is enumerated, in case an aggregate or virtual device
    exposes the raw elements where the built-in does not
  * whatever channels ARE granted get independent RMS, correlation and
    Whisper verdicts
  * with two or more channels, sum vs difference is the decisive test: if
    ``L+R`` is silent while ``L-R`` carries speech, that IS phase cancellation,
    stated arithmetically
  * a single channel still gets the full battery, and is compared against the
    known-good reference so "the mic is deaf to voices" is measured rather
    than assumed

Contention
----------
Another process holding the device changes what CoreAudio delivers — measured
earlier in this investigation at 6.5x on peak. The profiler refuses to report
numbers while a known audio owner is live, because a measurement taken under
contention is a measurement of the contention.

Usage
-----
    python3 scripts/hardware_acoustic_profiler.py            # 5s, speak into it
    python3 scripts/hardware_acoustic_profiler.py --seconds 8
    python3 scripts/hardware_acoustic_profiler.py --no-whisper
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:
    import numpy as np
except ImportError:                                    # pragma: no cover
    print("numpy is required", file=sys.stderr)
    raise SystemExit(2)


# ---------------------------------------------------------------------------
# Measurement helpers — the same statistics the flight recorder reports, so
# numbers from the two instruments are directly comparable (DRY: the metric
# definitions live in one place conceptually and are mirrored here rather than
# reinvented with different formulas).
# ---------------------------------------------------------------------------


def syllabic_modulation(x: np.ndarray, sr: int) -> float:
    """Fraction of envelope energy in the 2-8 Hz band — the rhythm of speech.

    Noise and steady tones do not modulate at syllable rate. This is the
    cheapest single number separating "a voice was present" from "energy was
    present", and it is what the capture forensics already keys its verdict
    on. Known-good speech measures ~0.45 here; the operator's captures
    measured 0.15-0.31."""
    if x.size < sr // 4:
        return 0.0
    hop = max(1, sr // 100)
    env = np.sqrt(np.convolve(x.astype(np.float64) ** 2,
                              np.ones(hop) / hop, mode="same")[::hop])
    if env.size < 16:
        return 0.0
    env = env - env.mean()
    spec = np.abs(np.fft.rfft(env * np.hanning(env.size)))
    freqs = np.fft.rfftfreq(env.size, hop / sr)
    band = spec[(freqs >= 2.0) & (freqs <= 8.0)].sum()
    total = spec[freqs >= 0.5].sum()
    return float(band / total) if total > 1e-12 else 0.0


def describe(x: np.ndarray, sr: int) -> Dict[str, Any]:
    if not x.size:
        return {"samples": 0}
    peak = float(np.max(np.abs(x)))
    rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    return {
        "samples": int(x.size),
        "duration_s": round(x.size / sr, 2),
        "peak": round(peak, 5),
        "rms": round(rms, 6),
        "crest_db": round(20 * float(np.log10(peak / rms)), 1) if rms > 1e-12 else 0.0,
        "over_full_scale": int(np.sum(np.abs(x) > 1.0)),
        "syllabic_modulation": round(syllabic_modulation(x, sr), 3),
    }


# ---------------------------------------------------------------------------
# Hardware interrogation
# ---------------------------------------------------------------------------


def enumerate_inputs(sd: Any) -> List[Dict[str, Any]]:
    """Every input device and the channel counts it will actually accept.

    Asked by ATTEMPTING each count rather than trusting ``max_input_channels``:
    the reported maximum and the accepted maximum are different questions, and
    an aggregate device may advertise one and grant another."""
    out: List[Dict[str, Any]] = []
    try:
        devices = sd.query_devices()
    except Exception as exc:                           # noqa: BLE001
        return [{"error": f"{type(exc).__name__}: {exc}"}]
    for idx, dev in enumerate(devices):
        if int(dev.get("max_input_channels", 0) or 0) <= 0:
            continue
        accepted = []
        for ch in (1, 2, 3, 4, 6, 8):
            try:
                sd.check_input_settings(
                    device=idx, channels=ch,
                    samplerate=int(dev.get("default_samplerate", 48000) or 48000),
                )
                accepted.append(ch)
            except Exception:                          # noqa: BLE001
                pass
        out.append({
            "index": idx,
            "name": dev.get("name", "?"),
            "reported_max_channels": int(dev.get("max_input_channels", 0) or 0),
            "accepted_channel_counts": accepted,
            "default_samplerate": int(dev.get("default_samplerate", 0) or 0),
        })
    return out


def detect_contention() -> List[str]:
    """Which known audio owners are live. A measurement taken while another
    process holds the device measures the contention, not the microphone."""
    owners: List[str] = []
    try:
        import subprocess
        ps = subprocess.run(
            ["ps", "-eo", "pid,command"], capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:                                  # noqa: BLE001
        return owners
    me = {str(os.getpid()), str(os.getppid())}
    for line in ps.splitlines():
        low = line.lower()
        pid = line.strip().split(" ", 1)[0]
        # Skip our own process tree and the shell that launched us: a command
        # line that MENTIONS the marker is not a process that OWNS the device.
        # The first version matched its own invocation and reported the
        # profiler as contention against itself.
        if pid in me or "hardware_acoustic_profiler" in low:
            continue
        if "grep" in low or low.lstrip().startswith(("/bin/zsh", "/bin/sh", "-zsh")):
            continue
        if "python" not in low:
            continue
        for marker in ("audio_plane_host", "unified_supervisor"):
            if marker in low:
                owners.append(line.strip()[:90])
                break
    return owners


def capture(sd: Any, seconds: float, channels: int,
            sr: int = 48000, device: Optional[int] = None,
            ) -> Tuple[Optional[np.ndarray], str]:
    """Record *channels* channels. Returns (array, error).

    The stream is closed in a ``finally`` under every outcome. A profiler that
    leaked the CoreAudio lock would wedge the very daemons this investigation
    is trying to keep alive — and this session has already wedged enough of
    them."""
    stream = None
    try:
        frames = int(sr * seconds)
        stream = sd.InputStream(
            samplerate=sr, channels=channels, dtype="float32", device=device,
        )
        stream.start()
        buf = np.zeros((frames, channels), dtype=np.float32)
        filled = 0
        deadline = time.monotonic() + seconds + 5.0
        while filled < frames and time.monotonic() < deadline:
            block, _overflowed = stream.read(min(2048, frames - filled))
            n = block.shape[0]
            buf[filled:filled + n] = block[:, :channels]
            filled += n
        return buf[:filled], ""
    except Exception as exc:                           # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        # Ordered and individually guarded: a failure to stop must not prevent
        # the close that actually releases the device.
        for step in ("stop", "close"):
            try:
                if stream is not None:
                    getattr(stream, step)()
            except Exception:                          # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Phase analysis — the decisive test, when the channels exist
# ---------------------------------------------------------------------------


def phase_report(chans: List[np.ndarray], sr: int) -> Dict[str, Any]:
    """Sum vs difference. If L+R is silent while L-R carries speech, the
    channels are phase-inverted and mono downmixing destroys the voice —
    stated arithmetically rather than inferred."""
    if len(chans) < 2:
        return {"applicable": False,
                "reason": "only one channel was granted — the array is "
                          "downmixed below the layer PortAudio can address"}
    a, b = chans[0], chans[1]
    n = min(a.size, b.size)
    a, b = a[:n], b[:n]
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    corr = float(np.dot(a, b) / denom) if denom > 1e-12 else 0.0
    summed, diffed = (a + b) / 2.0, (a - b) / 2.0
    s_rms = float(np.sqrt(np.mean(summed.astype(np.float64) ** 2)))
    d_rms = float(np.sqrt(np.mean(diffed.astype(np.float64) ** 2)))
    verdict = "channels are in phase — mono downmix is safe"
    if corr < -0.5:
        verdict = ("channels are ANTI-CORRELATED — mono downmix cancels the "
                   "signal; this is the phase-cancellation fault")
    elif d_rms > s_rms * 4.0:
        verdict = ("difference carries far more energy than the sum — "
                   "consistent with partial phase cancellation on downmix")
    return {
        "applicable": True,
        "correlation": round(corr, 4),
        "sum_rms": round(s_rms, 6),
        "difference_rms": round(d_rms, 6),
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# Whisper verification
# ---------------------------------------------------------------------------


def transcribe_each(chans: List[np.ndarray], sr: int) -> List[Dict[str, Any]]:
    """Feed every channel to Whisper INDEPENDENTLY.

    One channel transcribing while another hallucinates would isolate the
    active beamforming node. Reuses the same model id the streaming STT loads,
    so a verdict here means the same thing it would mean in the pipeline."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return [{"error": "faster_whisper not installed"}]
    try:
        model_id = os.getenv("JARVIS_STT_MODEL", "base")
        model = WhisperModel(model_id, device="cpu", compute_type="int8")
    except Exception as exc:                           # noqa: BLE001
        return [{"error": f"model load failed: {type(exc).__name__}: {exc}"}]

    results: List[Dict[str, Any]] = []
    for i, ch in enumerate(chans):
        x = ch
        if sr != 16000 and x.size:
            idx = np.linspace(0, x.size - 1, int(x.size * 16000 / sr))
            x = np.interp(idx, np.arange(x.size), x).astype(np.float32)
        entry: Dict[str, Any] = {"channel": i}
        for label, audio in (
            ("as_captured", x),
            ("normalized", x / max(float(np.max(np.abs(x))), 1e-9) * 0.5 if x.size else x),
        ):
            try:
                segs, _info = model.transcribe(
                    np.asarray(audio, dtype=np.float32), language="en",
                    beam_size=5, best_of=5, vad_filter=False,
                )
                segs = list(segs)
                entry[label] = " ".join(s.text.strip() for s in segs)[:120]
                entry[f"{label}_no_speech"] = (
                    round(float(segs[0].no_speech_prob), 3) if segs else None
                )
            except Exception as exc:                   # noqa: BLE001
                entry[label] = f"<error {type(exc).__name__}>"
        results.append(entry)
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--samplerate", type=int, default=48000)
    ap.add_argument("--no-whisper", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="measure even while another audio owner is live")
    ap.add_argument("--device", type=int, default=None,
                    help="input device index. DEFAULTS TO THE SYSTEM DEFAULT, "
                         "which on macOS may be a Continuity microphone — an "
                         "iPhone named after its owner, sitting in a pocket. "
                         "Run --list-devices first.")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        import sounddevice as sd
    except ImportError:
        print("sounddevice is required (pip install sounddevice)", file=sys.stderr)
        return 2

    report: Dict[str, Any] = {"schema_version": "acoustic_profile.1"}

    owners = detect_contention()
    report["contention"] = owners
    if owners and not args.force:
        print("⚠  Another audio owner is live — a measurement taken now would")
        print("   describe the contention, not the microphone. Stop it, or")
        print("   re-run with --force if you intend to measure contended.")
        for o in owners:
            print(f"     {o}")
        return 3

    report["devices"] = enumerate_inputs(sd)
    try:
        default_in = sd.query_devices(kind="input")
        report["default_input"] = default_in.get("name", "?")
    except Exception:                                  # noqa: BLE001
        report["default_input"] = "?"
    chosen = args.device
    report["device_used"] = chosen if chosen is not None else f"default ({report['default_input']!r})"
    # A Continuity microphone — an iPhone or iPad — is named after its owner
    # and can silently become the system default when the phone comes into
    # range. Measuring it instead of the built-in array produces exactly the
    # "27x too quiet, no speech rhythm" signature this script hunts, so the
    # ambiguity is called out rather than left for the reader to notice.
    if chosen is None:
        for d in report["devices"]:
            if d.get("name") == report["default_input"] and "macbook" not in str(d.get("name", "")).lower():
                print(f"⚠  The system default input is {d.get('name')!r} "
                      f"(index {d.get('index')}).")
                print("   That is not the built-in array. On macOS a device named")
                print("   after a person is a Continuity microphone — a phone or")
                print("   tablet, wherever it happens to be. Re-run with")
                print(f"   --device <index> to measure a specific microphone.")
                break
    if args.list_devices:
        for d in report["devices"]:
            print(f"   [{d.get('index')}] {d.get('name')!r} accepted={d.get('accepted_channel_counts')}")
        return 0
    if not args.json:
        print("── input devices ──")
        for d in report["devices"]:
            print(f"   [{d.get('index')}] {d.get('name')!r}")
            print(f"        reported_max={d.get('reported_max_channels')} "
                  f"accepted={d.get('accepted_channel_counts')}")

    # Ask for stereo FIRST, per the hypothesis. Fall back only on refusal, and
    # record which happened — "the hardware refused" is the finding.
    granted, data, err = 0, None, ""
    for want in (2, 1):
        data, err = capture(sd, args.seconds, want, args.samplerate, args.device)
        if data is not None and data.size:
            granted = want
            break
        report.setdefault("capture_attempts", []).append(
            {"channels": want, "error": err})
    report["channels_granted"] = granted
    if data is None or not data.size:
        print(f"✖ capture failed entirely: {err}")
        report["fatal"] = err
        if args.json:
            print(json.dumps(report, indent=2))
        return 4

    if not args.json:
        print(f"\n── captured {args.seconds}s at {args.samplerate}Hz, "
              f"{granted} channel(s) granted ──")
        if granted < 2:
            print("   NOTE: stereo refused. The array is downmixed below the")
            print("   layer PortAudio addresses, so per-element phase cannot")
            print("   be inspected here. The hypothesis is untestable on this")
            print("   device — not disproven.")

    # IndexError-proof: slice by what was actually granted, never by what was
    # requested. This is the guard the mandate asked for, and it is the reason
    # a stereo-shaped analysis cannot crash on a mono capture.
    chans: List[np.ndarray] = []
    for i in range(min(granted, data.shape[1] if data.ndim > 1 else 1)):
        try:
            chans.append(np.ascontiguousarray(data[:, i]))
        except (IndexError, ValueError):
            break
    if not chans:
        chans = [data.reshape(-1)]

    report["channels"] = [describe(c, args.samplerate) for c in chans]
    report["phase"] = phase_report(chans, args.samplerate)

    if not args.json:
        print()
        for i, st in enumerate(report["channels"]):
            print(f"   ch{i}: peak={st['peak']:<9} rms={st['rms']:<10} "
                  f"crest={st['crest_db']}dB  modulation={st['syllabic_modulation']}")
        print(f"\n── phase ──\n   {report['phase'].get('verdict') or report['phase'].get('reason')}")
        if report["phase"].get("applicable"):
            print(f"   correlation={report['phase']['correlation']} "
                  f"sum_rms={report['phase']['sum_rms']} "
                  f"diff_rms={report['phase']['difference_rms']}")

    if not args.no_whisper:
        report["transcription"] = transcribe_each(chans, args.samplerate)
        if not args.json:
            print("\n── whisper, each channel independently ──")
            for entry in report["transcription"]:
                if "error" in entry:
                    print(f"   {entry['error']}")
                    continue
                print(f"   ch{entry['channel']} as-captured : "
                      f"{entry.get('as_captured')!r}")
                print(f"   ch{entry['channel']} normalized  : "
                      f"{entry.get('normalized')!r}")
                print(f"   ch{entry['channel']} no_speech_prob: "
                      f"{entry.get('as_captured_no_speech')}")

    # Interpretation, against the reference measured earlier in this hunt.
    if not args.json:
        ref_mod, ref_rms = 0.44, 0.079
        got = report["channels"][0]
        print("\n── reading ──")
        print(f"   known-good speech (played probe): modulation {ref_mod}, rms {ref_rms}")
        print(f"   this capture                   : modulation "
              f"{got['syllabic_modulation']}, rms {got['rms']}")
        if got["syllabic_modulation"] < ref_mod * 0.6:
            print("   → the capture lacks speech RHYTHM, not merely level.")
            print("     Amplification cannot recover this; the voice is not")
            print("     arriving. Look at macOS mic mode (Voice Isolation),")
            print("     input source, and distance before any more code.")
        else:
            print("   → speech rhythm is present. If whisper still returns")
            print("     nothing, the fault is downstream of capture.")

    # ALWAYS persist. A diagnostic whose only output is stdout forces the
    # operator to copy-paste it back, and that has now failed repeatedly in
    # this investigation — three separate runs whose numbers were lost between
    # the terminal and the analysis. The report is written where it can simply
    # be read.
    try:
        out_dir = os.path.join(REPO_ROOT, ".jarvis", "acoustic_profile")
        os.makedirs(out_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        report["captured_at"] = stamp
        for name in (f"profile-{stamp}.json", "latest.json"):
            with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2)
        if not args.json:
            print(f"\n   report written: .jarvis/acoustic_profile/latest.json")
    except (OSError, TypeError, ValueError) as exc:
        if not args.json:
            print(f"\n   (could not persist report: {type(exc).__name__})")

    if args.json:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
